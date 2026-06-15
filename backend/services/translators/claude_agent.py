"""Claude Agent SDK-backed translator.

Uses `claude_agent_sdk.query` — an in-process, stateless one-shot interface to
the same `claude` binary the `claude_cli` backend invokes via subprocess. Same
subscription auth, same models (Opus / Sonnet) — but every chapter reuses the
underlying connection / Node.js runtime instead of paying the 5-10s cold-start
cost on each call. That alone is the bulk of the wall-time win on a multi-
hundred-chapter sweep.

Statelessness matters: each call is a fresh `query()` invocation with the full
prompt rebuilt by `build_prompt()` from the current glossary. There is no
conversation history between chapters, so live glossary edits via the web UI
take effect on the very next chapter (same semantics as the CLI backend) and
quality cannot "drift over a long conversation" because no conversation exists.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLIConnectionError,
    ProcessError,
    RateLimitEvent,
    ResultMessage,
    TextBlock,
    query,
)

from backend.config import (
    CLAUDE_AGENT_CALL_TIMEOUT,
    CLAUDE_AGENT_TRANSLATOR_EFFORT,
    CLAUDE_AGENT_TRANSLATOR_MODEL,
    CLAUDE_CLI_PATH,
)
from backend.services.providers import Provider, resolve_secret
from backend.services.translators._subprocess_utils import (
    claude_sdk_env_overrides,
    resolve_binary,
    system_prompt_file_for,
)

from ._claude_errors import classify as _classify_error_string
from .base import (
    BACKOFF_SCHEDULE,
    BaseTranslator,
    TransientTranslatorError,
)

logger = logging.getLogger(__name__)

# Hard upper bound on a single chapter call. The SDK does the same Node.js
# subprocess work the CLI backend does, just amortized — give the same
# headroom for cold-disk / first-call warmup as the CLI backend. Sourced from
# config (CLAUDE_AGENT_CALL_TIMEOUT, default 600s) so a slow chapter can be
# given headroom via env without a rebuild; was previously a hardcoded 600.
_CALL_TIMEOUT_SECONDS = CLAUDE_AGENT_CALL_TIMEOUT

# Effort levels the Agent SDK accepts (blank = omit the option). Mirrors the
# validation set in config.py so a per-provider params["effort"] override is
# checked the same way.
_VALID_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})

# Extended thinking ("effort") can split one chapter response across more than
# one SDK turn (a thinking turn, then the text turn), so a hard max_turns=1
# makes the SDK abort a thinking model mid-response with "Reached maximum number
# of turns (1)". That bug kept claude_agent on opus-4-5 from ever completing a
# chapter (opus-4-8 happened to finish within one turn, so it slipped through).
# allowed_tools=[] below means the model cannot enter a real tool loop, so these
# extra turns are pure headroom for thinking to finish, never runaway agentic
# behavior; the model still stops at its own end_turn well before this cap. The
# claude_cli backend keeps --max-turns 1 because it runs without thinking-config.
_MAX_TURNS = 8


# The genre brief is passed to the SDK as a system-prompt FILE (see
# `system_prompt_file_for` in `_subprocess_utils`): an inline --system-prompt
# would overflow the ~8 KB Windows command-line cap and hang the subprocess.


class ClaudeAgentError(Exception):
    """Permanent failure from the Claude Agent SDK — authentication, billing,
    bad model name, malformed request. Not recoverable inside the current
    subscription window; surfaces to the caller as a hard chapter error so the
    user sees a clear actionable message instead of a silent retry loop."""


def _log_claude_usage(role: str, msg: object) -> None:
    """Surface Claude Code's per-call usage so the user can verify prompt
    caching is firing across a sweep.

    The Claude Agent SDK / CLI route through Claude Code subscription, which
    applies prompt caching server-side when the system prompt is byte-stable
    (which it is — we write it once to a file and reuse it). When caching
    fires, `usage.cache_read_input_tokens` reports the cached prefix; on the
    first call of a 5-minute window, `cache_creation_input_tokens` is non-
    zero instead. Logging both gives the user proof the cache is working."""
    usage = getattr(msg, "usage", None) or {}
    if not isinstance(usage, dict):
        return
    input_t = usage.get("input_tokens") or 0
    output_t = usage.get("output_tokens") or 0
    cache_read = usage.get("cache_read_input_tokens") or 0
    cache_create = usage.get("cache_creation_input_tokens") or 0
    cost = getattr(msg, "total_cost_usd", None)
    if cache_read or cache_create:
        logger.info(
            "claude_agent %s usage: input=%d, output=%d, cache_read=%d, cache_create=%d%s",
            role, input_t, output_t, cache_read, cache_create,
            f", cost=${cost:.4f}" if cost else "",
        )
    else:
        logger.debug(
            "claude_agent %s usage: input=%d, output=%d (no cache hit)%s",
            role, input_t, output_t,
            f", cost=${cost:.4f}" if cost else "",
        )


def _error_payload(e: BaseException) -> str:
    """Build the full classification surface for an SDK exception.

    `str(e)` on Windows ProcessError typically yields the bland "Command
    'claude' returned non-zero exit status 1" — the actual CLI 429 body sits
    on `.stderr` / `.output` (the SDK keeps subprocess fields verbatim).
    Without inspecting those, rate-limit detection effectively never fires
    on a Windows install."""
    parts: list[str] = [str(e)]
    for attr in ("stderr", "output", "result"):
        val = getattr(e, attr, None)
        if val is None:
            continue
        if isinstance(val, (bytes, bytearray)):
            try:
                parts.append(val.decode("utf-8", errors="replace"))
            except Exception:
                parts.append(repr(val))
        else:
            parts.append(str(val))
    args = getattr(e, "args", None) or ()
    for a in args:
        if isinstance(a, str):
            parts.append(a)
    return "\n".join(parts)


class ClaudeAgentTranslator(BaseTranslator):
    name = "claude_agent"
    model_id = CLAUDE_AGENT_TRANSLATOR_MODEL or "default"
    # Forced serial: every call burns the user's Claude subscription window.
    max_parallel = 1

    def cache_identity(self) -> str:
        """Fold the model bump (4.5 → 4.7) and the thinking-effort level into
        the cache key so stale entries produced under the old config can't
        collide with the new ones. The system_instruction text is also a cache-
        key field, so SYSTEM_INSTRUCTION edits are an independent second
        invalidation path; this token covers dimensions that don't show up in
        the system-instruction body. Mirrors DeepSeek's revN pattern."""
        return (
            f"{self.name}:{self.model_id}"
            f":opus47-think{self._effort or 'off'}"
        )

    def __init__(self, provider: Provider | None = None) -> None:
        # Claude Agent SDK authenticates via the local Claude install (the
        # user's subscription), so there's no API key to thread through.
        # The Provider only contributes model_id — different rows can target
        # different Claude versions (4.6 / 4.7 / future). The cache key in
        # cache_identity reads self.model_id so model-version changes do not
        # collide in llm_cache.
        if provider is not None and provider.model_id:
            self.model_id = provider.model_id
        # Subscription OAuth token (`claude setup-token`) for the 2026-06-15
        # credit-pool change — resolved from the provider's secret_ref and
        # injected as CLAUDE_CODE_OAUTH_TOKEN into the SDK's CLI subprocess
        # (see _sdk_core / claude_subprocess_env). NULL secret_ref → None →
        # local `claude login` session (or an inherited env var). Never an
        # API key: this is a subscription backend.
        self._oauth_token = resolve_secret(provider) if provider is not None else None
        # Thinking-effort is PER-PROVIDER (provider.params["effort"]), falling
        # back to the global CLAUDE_AGENT_TRANSLATOR_EFFORT default. This lets a
        # Sonnet provider run at a lower effort than the Opus-tuned global
        # default without changing the shipped default: effort=high makes
        # Sonnet 4.6 spiral into runaway extended thinking (250k+ output
        # tokens/chapter, blowing the call timeout), so the user can dial it
        # down per provider. Invalid / absent values fall through to the global.
        provider_effort = None
        if provider is not None:
            raw = provider.params.get("effort")
            if isinstance(raw, str) and raw.strip().lower() in _VALID_EFFORTS:
                provider_effort = raw.strip().lower()
        self._effort = provider_effort or CLAUDE_AGENT_TRANSLATOR_EFFORT
        self._semaphore = asyncio.Semaphore(self.max_parallel)
        # System-prompt file is written lazily per call (one file per
        # genre+brief hash) because the system instruction is now genre-aware
        # and varies across calls. See system_prompt_file_for.

    async def _complete(self, prompt: str) -> str:
        return await self._call_sdk(prompt, with_system=True)

    async def _complete_plain(self, prompt: str) -> str:
        return await self._call_sdk(prompt, with_system=False)

    async def _call_sdk(self, user_prompt: str, with_system: bool) -> str:
        async with self._semaphore:
            return await self._call_sdk_with_retry(user_prompt, with_system)

    async def _call_sdk_with_retry(self, user_prompt: str, with_system: bool) -> str:
        last_exc: BaseException | None = None
        for attempt in range(len(BACKOFF_SCHEDULE) + 1):
            try:
                return await asyncio.wait_for(
                    self._run_sdk(user_prompt, with_system),
                    timeout=_CALL_TIMEOUT_SECONDS,
                )
            except (ClaudeAgentError, TransientTranslatorError):
                # Both are classified terminal-for-this-attempt outcomes.
                # ClaudeAgentError = permanent, retrying inside the window
                # won't help. TransientTranslatorError = already-classified
                # rate-limit; the caller's /start path will reset and re-queue.
                raise
            except (
                asyncio.TimeoutError,
                CLIConnectionError,
                ProcessError,
                ConnectionError,
                OSError,
            ) as e:
                last_exc = e
                # The SDK surfaces a CLI subprocess rate-limit failure as a
                # ProcessError whose stderr/stdout contains the CLI's
                # rate-limit body (e.g. "You've hit your limit · resets …" or
                # an api_error_status 429 envelope). Inspect every field, not
                # just str(e) — on Windows str(e) is the bland exit-status
                # message and the rate-limit body is on .stderr.
                err_text = _error_payload(e)
                err_lower = err_text.lower()
                if (
                    "hit your limit" in err_lower
                    or '"api_error_status":429' in err_text
                    or "api_error_status=429" in err_text
                    or "api_error_status':429" in err_text
                    or _classify_error_string(err_text) == "rate_limit"
                ):
                    logger.warning("Claude Agent SDK rate-limited (detected in ProcessError): %s", err_text[:300])
                    raise TransientTranslatorError(
                        "Claude subscription rate limit hit. Wait for your 5-hour "
                        "window to reset, then click Start to resume. "
                        f"SDK message: {err_text.strip()[:300]}"
                    ) from e
                if _classify_error_string(err_text) == "auth":
                    logger.error("Claude Agent SDK auth failure (detected in ProcessError): %s", err_text[:300])
                    raise ClaudeAgentError(
                        "Claude Agent SDK is not authenticated. Run `claude` in a "
                        "terminal to log in, then restart the server. "
                        f"SDK message: {err_text.strip()[:300]}"
                    ) from e
                if attempt >= len(BACKOFF_SCHEDULE):
                    break
                delay = BACKOFF_SCHEDULE[attempt]
                logger.warning(
                    "Claude Agent SDK transient error (attempt %d/%d): %s — retrying in %.1fs",
                    attempt + 1,
                    len(BACKOFF_SCHEDULE) + 1,
                    e,
                    delay,
                )
                await asyncio.sleep(delay)
        # Include the underlying exception's type + message in the surfaced
        # error so the user (and the /start reset SQL) can see WHY the SDK
        # bailed — "temporarily unavailable" alone hides whether this was a
        # CLI subprocess crash, a connect timeout, a Windows OSError, etc.
        cause = f"{type(last_exc).__name__}: {last_exc}" if last_exc else "unknown"
        raise TransientTranslatorError(
            "Claude Agent SDK temporarily unavailable. The chapter is unchanged — "
            f"try Retranslate later. Last error: {cause[:300]}"
        ) from last_exc

    async def _run_sdk(self, user_prompt: str, with_system: bool) -> str:
        """Dispatch the SDK call so it runs on a ProactorEventLoop.

        uvicorn pins its main loop to WindowsSelectorEventLoopPolicy (it needs
        loop.add_reader for socket I/O), but the claude-agent SDK uses
        anyio.open_process → asyncio.create_subprocess_exec, which raises
        NotImplementedError on the Selector loop on Windows. Running the SDK
        call in a worker thread with its own ProactorEventLoop sidesteps the
        conflict without forcing uvicorn off Selector. On non-Windows there's
        no policy conflict, so we just await the coroutine directly."""
        if sys.platform != "win32":
            return await self._sdk_core(user_prompt, with_system)
        return await asyncio.to_thread(
            self._run_sdk_in_proactor_loop, user_prompt, with_system
        )

    def _run_sdk_in_proactor_loop(self, user_prompt: str, with_system: bool) -> str:
        loop = asyncio.ProactorEventLoop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(self._sdk_core(user_prompt, with_system))
        finally:
            try:
                loop.close()
            finally:
                # Clear the loop on this thread so a future to_thread() call
                # that lands on the same worker doesn't try to reuse a closed
                # loop.
                asyncio.set_event_loop(None)

    async def _sdk_core(self, user_prompt: str, with_system: bool) -> str:
        def _log_stderr(line: str) -> None:
            # Always drain the CLI subprocess's stderr — when it's not piped,
            # the child can block on a full OS stderr buffer on Windows and
            # the SDK's initialize control request then times out. Routing it
            # through our logger also captures useful diagnostics on failure.
            line = line.rstrip()
            if line:
                logger.debug("claude-agent stderr: %s", line)

        options = ClaudeAgentOptions(
            cli_path=resolve_binary(CLAUDE_CLI_PATH),
            model=self.model_id if self.model_id and self.model_id != "default" else None,
            # Thinking-effort level. "high" (default) enables Opus 4.8 extended
            # thinking — the model deliberates before emitting prose, which is
            # the lever for higher first-pass fidelity and more natural English.
            # "xhigh" is Opus-4.7-exclusive and goes deeper. Blank → omit so
            # the SDK's own default applies; "low" effectively disables it.
            # ThinkingBlock items the SDK streams are filtered out below by
            # the `isinstance(block, TextBlock)` check, so thinking output never
            # lands in the envelope payload. Per-provider (self._effort): a
            # Sonnet provider can run lower than the Opus-tuned global default.
            effort=self._effort or None,
            # Pass the system prompt as a file reference, not an inline string —
            # see system_prompt_file_for in _subprocess_utils for the cap rationale.
            # File path derives from a hash of self.system_instruction, so a
            # genre swap routes to a different file without touching the cache
            # of pre-existing files (warm prompt-cache server-side, when the
            # path is unchanged across calls).
            system_prompt=(
                {"type": "file", "path": str(system_prompt_file_for(self.system_instruction))}
                if with_system
                else None
            ),
            stderr=_log_stderr,
            # Headroom for extended thinking to finish (see _MAX_TURNS). A hard
            # cap of 1 aborts a thinking model mid-response; allowed_tools=[]
            # keeps the extra turns safe (no tool loop possible).
            max_turns=_MAX_TURNS,
            # No tools. We want a pure text-in-text-out call; Claude should
            # not try to read files, run commands, or invoke MCP servers.
            allowed_tools=[],
            # Defensive: even with allowed_tools=[], explicitly deny anything
            # the SDK might try to enable by default. `dontAsk` means anything
            # not pre-approved is denied silently (no interactive prompt).
            permission_mode="dontAsk",
            # Empty list (not None!) tells the CLI to load NO settings sources.
            # None means "use CLI defaults" which on this user's machine pulls
            # in their MCP servers, skills, agents, and project config — adding
            # ~10 KB to the system prompt and burning subscription quota on
            # context the translator does not need. Empty list keeps the call
            # isolated and reproducible.
            setting_sources=[],
            # Same reason: empty MCP config + strict mode means the user's
            # MCP servers (Gmail, Drive, Calendar, etc.) do not appear in the
            # CLI's tool list at all.
            mcp_servers={},
            strict_mcp_config=True,
            # Don't load Claude Code skills or sub-agents.
            skills=[],
            agents={},
            include_partial_messages=False,
            # Inject the subscription OAuth token (post-credit-pool auth) into
            # the CLI subprocess the SDK spawns. The SDK merges this on top of
            # the inherited env, and CLAUDE_CODE_OAUTH_TOKEN is auth precedence
            # #1, so the call runs on the subscription's Agent-SDK credit, never
            # API credits. Token-only override (not a full env). See
            # claude_sdk_env_overrides.
            env=claude_sdk_env_overrides(self._oauth_token),
        )
        result_parts: list[str] = []
        async for msg in query(prompt=user_prompt, options=options):
            if isinstance(msg, AssistantMessage):
                if msg.error:
                    self._raise_assistant_error(msg.error)
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        result_parts.append(block.text)
            elif isinstance(msg, ResultMessage):
                if msg.is_error:
                    self._raise_result_error(msg)
                _log_claude_usage("translator", msg)
                # Plumb usage into the BaseTranslator accumulator. Claude
                # Agent SDK reports cache_read_input_tokens (the cached
                # prefix); cache_creation_input_tokens are billed at full
                # rate so they belong in input_tokens, not cached.
                usage = getattr(msg, "usage", None) or {}
                if isinstance(usage, dict):
                    self._emit_usage(
                        input_tokens=usage.get("input_tokens") or 0,
                        output_tokens=usage.get("output_tokens") or 0,
                        cached_input_tokens=usage.get("cache_read_input_tokens") or 0,
                    )
            elif isinstance(msg, RateLimitEvent):
                # Informational notification — the SDK still raises a proper
                # error via AssistantMessage.error / ResultMessage on actual
                # cap. Log so we can see how close we're running.
                logger.info("Claude Agent SDK rate-limit event: %s", msg.rate_limit_info)
        if not result_parts:
            raise ClaudeAgentError(
                "Claude Agent SDK returned no text content (empty response)"
            )
        return "".join(result_parts)

    def _raise_assistant_error(self, err: str) -> None:
        if err == "rate_limit":
            raise TransientTranslatorError(
                "Claude subscription rate limit hit. Wait for your 5-hour "
                "window to reset, then click Start to resume."
            )
        if err == "authentication_failed":
            raise ClaudeAgentError(
                "Claude Agent SDK is not authenticated. Run `claude` in a "
                "terminal to log in, then restart the server."
            )
        if err == "billing_error":
            raise ClaudeAgentError(
                "Claude subscription billing issue. Check your account before retrying."
            )
        if err == "server_error":
            raise TransientTranslatorError(
                "Claude upstream server error. Click Translate again later to retry."
            )
        # invalid_request / unknown / anything else — permanent for this call.
        raise ClaudeAgentError(f"Claude Agent SDK error: {err}")

    def _raise_result_error(self, msg: ResultMessage) -> None:
        status = msg.api_error_status
        # `errors` is the most direct text source; fall back to result.
        snippet_parts = msg.errors or []
        if msg.result:
            snippet_parts = list(snippet_parts) + [msg.result]
        snippet = " | ".join(s for s in snippet_parts if s).strip()[:300]
        if status == 429:
            raise TransientTranslatorError(
                f"Claude rate limit (HTTP 429). Wait for window reset. {snippet}"
            )
        if status in (401, 403):
            raise ClaudeAgentError(
                f"Claude auth failure (HTTP {status}). Re-run `claude` to log in. {snippet}"
            )
        if status and 500 <= status <= 599:
            raise TransientTranslatorError(
                f"Claude server error (HTTP {status}) — click Translate to retry. {snippet}"
            )
        # No HTTP status — fall back to pattern matching the snippet.
        cls = _classify_error_string(snippet)
        if cls == "rate_limit":
            raise TransientTranslatorError(
                f"Claude rate limit detected from result. {snippet}"
            )
        if cls == "auth":
            raise ClaudeAgentError(
                f"Claude auth failure detected from result. {snippet}"
            )
        raise ClaudeAgentError(f"Claude Agent SDK result error: {snippet}")


async def probe_sdk() -> None:
    """Lightweight probe at server startup: just verify the underlying CLI is
    installed and resolvable. The SDK uses the same `claude` binary, so the
    CLI's `--version` check is a sufficient sanity test without burning
    subscription quota on a real round-trip. Auth issues will surface with a
    clear classified error on the first real translation."""
    # Reuse the CLI probe — it already validates path + version.
    from backend.services.translators.claude_cli import probe_cli
    await probe_cli()
    # Quick SDK-side check: confirm the package can be imported and the
    # resolved CLI path is honored by ClaudeAgentOptions construction. Also
    # exercises `effort=` so a future SDK schema break on that field surfaces
    # at boot rather than on the first chapter call.
    try:
        _ = ClaudeAgentOptions(
            cli_path=resolve_binary(CLAUDE_CLI_PATH),
            max_turns=1,
            effort=CLAUDE_AGENT_TRANSLATOR_EFFORT or None,
        )
    except Exception as e:
        raise RuntimeError(
            f"Claude Agent SDK initialization failed: {e}. The "
            "`claude-agent-sdk` package may be incompatible with your Python "
            "version — try `pip install --upgrade claude-agent-sdk`."
        ) from e
    logger.info("Claude Agent SDK initialized (cli_path=%s)", resolve_binary(CLAUDE_CLI_PATH))
