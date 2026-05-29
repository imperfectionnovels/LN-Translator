# Backend tuning notes

Per-backend env vars and quirks. The defaults in `backend/config.py` are tuned
for the common case (`claude_agent`, Opus 4.7, `effort=high`); this doc covers
the knobs and the reasons they exist.

For the list of every var, see `.env.example`. This doc is the "why."

## claude_agent (default)

In-process Claude Agent SDK, running against the local `claude` subscription.
Default: Opus 4.7 with `effort=high` extended thinking.

| Var | Default | Notes |
|---|---|---|
| `CLAUDE_AGENT_TRANSLATOR_MODEL` | `claude-opus-4-7` | Don't downgrade; extended thinking is plumbed for 4.7 specifically. |
| `CLAUDE_AGENT_TRANSLATOR_EFFORT` | `high` | `low` / `medium` / `high` / `xhigh` / `max`. `xhigh` goes deeper but eats more quota; `low` effectively disables thinking. |
| `CLAUDE_AGENT_CALL_TIMEOUT` | `600` | Long-chapter Opus + thinking finishes inside 8 min. Longer wait = hung call. |

The SDK call is text-in / text-out — no provider schema enforcement. The
backend uses the delimited envelope (`TITLE_EN: ...\n=====BODY=====\n...\n=====TERMS=====\n...`)
so the chapter body rides as raw text rather than an escaped JSON string.

`ThinkingBlock` items the SDK streams are filtered out by the
`isinstance(block, TextBlock)` check in `_sdk_core`; thinking text never lands
in the envelope payload.

The system prompt is large (~54 KB) and genre-aware: per call,
`build_system_instruction(genre, custom_brief)` composes base.md + the
genre overlay + the genre examples + the optional user brief. The composed
text is written to a per-content-hash file under
`USER_DATA_ROOT/runtime/translator_system_prompt-<hash>.txt` (one file per
distinct system prompt, reused across calls) and passed via
`system_prompt={"type": "file", "path": ...}` — the `--system-prompt-file`
CLI flag — because the inline `--system-prompt` arg overflowed Windows'
~8 KB command-line cap.

`cache_identity` includes the model + effort level (`opus47-think{effort}`),
so a model bump or effort change invalidates cached translations cleanly.

## claude_cli

Subprocess against the local `claude` binary. Same subscription as
`claude_agent`. **Has no thinking-config surface**, so quality is below the
SDK backend at default settings. Kept as a fallback when the SDK has issues.

| Var | Default | Notes |
|---|---|---|
| `CLAUDE_CLI_PATH` | `claude` | Path to the binary on PATH. On Windows the npm shim is `claude.CMD` — `_resolve_cli_path` uses `shutil.which` so PATHEXT is applied. |
| `CLAUDE_CLI_TRANSLATOR_MODEL` | `claude-opus-4-5` | Default is 4.5 because the CLI has no `--effort` plumbing. Switch to `claude_agent` for 4.7 + thinking. |

Implementation: subprocess via `subprocess.Popen` + `asyncio.to_thread(proc.communicate)`.
Uvicorn defaults to the Selector event loop on Windows, which raises
`NotImplementedError` for `asyncio.create_subprocess_exec` — the Popen route
works everywhere and lets `proc.kill()` clean up on cancellation so an orphan
`claude` process doesn't keep eating subscription quota.

`.cmd` / `.bat` files can no longer be executed directly on Python 3.13+;
`_build_cli_argv` wraps them through `cmd /c`.

Rate-limit substrings (`usage limit`, `5-hour limit`, `try again later`) raise
`TransientTranslatorError`; auth strings (`/login`, `not authenticated`) raise
`ClaudeCliError`.

## gemini

Gemini API via the async `google-genai` client.

| Var | Default | Notes |
|---|---|---|
| `GEMINI_API_KEY` | — | Required when this backend is selected. |
| `GEMINI_TRANSLATOR_MODEL` | `gemini-3-pro-preview` | Pro is the highest-fidelity Gemini for CN→EN. |
| `GEMINI_REQUEST_TIMEOUT` | `240` | Per-call timeout; a literary chapter completes well inside. |

Backoff schedule: `(2.0, 5.0, 12.0)` from `BACKOFF_SCHEDULE` in `base.py`.
Transient error classification (`_is_transient`) covers 408 / 429 / 5xx +
UNAVAILABLE / RESOURCE_EXHAUSTED / DEADLINE_EXCEEDED.

Safety blocks (`SAFETY` / `RECITATION` / `PROHIBITED_CONTENT` / `SPII` /
`BLOCKLIST`) raise `_GeminiBlocked` immediately — no retry, surfaced to the
user with a clear actionable message.

`MAX_TOKENS` with empty body is `_GeminiTruncatedEmpty` — transient, retried
once.

## deepseek

OpenAI-compatible API at `api.deepseek.com`. A single translation pass per
chapter on the provider's model (delimited free-form envelope, not JSON mode),
with a parse-retry and a plain-text fallback. A second polish pass, when wanted,
is configured as a per-novel refinement provider (`refinement_provider_id`),
which can point at DeepSeek or any other provider; DeepSeek the translator no
longer runs an internal revise stage.

| Var | Default | Notes |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | Required. |
| `DEEPSEEK_TRANSLATOR_MODEL` | `deepseek-v4-pro` | The translation model. |
| `DEEPSEEK_TRANSLATOR_TEMPERATURE` | `0.7` | Sampling temperature for the translation call. |
| `DEEPSEEK_MAX_OUTPUT_TOKENS` | `8192` | Hard `max_tokens`. A response that hits the cap fails the chapter rather than committing a truncated translation. |
| `DEEPSEEK_REQUEST_TIMEOUT` | `240` | Per-request timeout. |

`cache_identity` folds the model + temperature into the cache key; the
genre-aware system instruction is in the content-addressed key separately, so a
prompt edit invalidates cached translations automatically.

DeepSeek auto-caches the glossary-heavy prefix server-side. Log line
`deepseek X usage: prompt=N (cached=M), completion=...` shows the hit rate.

## Other backends

The four detailed backends above (plus `google_translate_free` further down)
are the ones with real per-backend tuning surface. The catalog in
`backend/services/translator_catalog.py::_CATALOG` also lists 14 more types,
most of which are thin OpenAI-compatible wrappers with no custom knobs. They live under `backend/services/translators/openai_compatible.py` (the
shared base) plus a tiny per-vendor subclass that sets `name`,
`DEFAULT_BASE_URL`, and the secret-ref hint. The catalog is the authoritative
menu; `KNOWN_PROVIDER_TYPES`, the Add Provider dropdown, and the factory
dispatch all derive from it.

Grouped by the catalog's `group` field:

- **Subscription** (no API key, vendor CLI handles auth out-of-band):
  - `codex_cli` — OpenAI Codex CLI, drives a ChatGPT Plus / Pro / Team
    subscription. Install with `npm i -g @openai/codex`, then `codex login`.
    Curated models: `gpt-5`, `gpt-5-codex`, `o3`.
  - `gemini_cli` — Google Gemini CLI, drives a Google account (free tier +
    Gemini Advanced). Install with `npm i -g @google/gemini-cli`, then run
    `gemini` once to OAuth.
  - `opencode` — multi-provider router by OpenCode. Routes to Anthropic /
    OpenAI / Google / GitHub Copilot under whichever provider the user has
    logged into. Model IDs use OpenCode's `<provider>/<model>` namespacing
    (e.g. `anthropic/claude-opus-4-7`).
- **API key** (paste a key; secret resolved via `keyring` → env-var fallback):
  - `anthropic_api` — Anthropic Claude via direct API key (alternative to the
    SDK / CLI subscription path).
  - `openai` — OpenAI native (`api.openai.com/v1`). Curated: GPT-5, GPT-5 mini,
    GPT-4.1, GPT-4o, o3, o3-mini.
  - `xai` — xAI Grok (`api.x.ai/v1`). Curated: Grok 4, Grok 3, Grok 3 mini.
  - `mistral` — Mistral (`api.mistral.ai/v1`). Curated: Large, Medium, Small
    `*-latest`.
  - `openrouter` — multi-model aggregator (`openrouter.ai/api/v1`). Model IDs
    use the `provider/model` form (e.g. `anthropic/claude-opus-4`).
  - `qwen` — Alibaba Qwen via the international DashScope OpenAI-compatible
    endpoint. Curated: Qwen Max / Plus / Turbo.
  - `zhipu` — Zhipu GLM (`open.bigmodel.cn/api/paas/v4`). Curated: GLM-4.6,
    GLM-4 Plus, GLM-4 Air.
  - `moonshot` — Moonshot Kimi (`api.moonshot.cn/v1`). Curated: Kimi K2,
    Kimi K1.5.
  - `groq` — Groq inference (`api.groq.com/openai/v1`). Curated:
    Llama 3.3/3.1 70B Versatile, Mixtral 8x7B.
  - `openai_compatible` — generic fallback for any vendor exposing the OpenAI
    `/chat/completions` shape. User sets the Base URL themselves; no curated
    model list.
- **Local / Free** (no API key):
  - `ollama` — Ollama local server (`http://localhost:11434/v1` by default).
    Fully offline. Pull the model with `ollama pull <name>` first. Curated
    suggestions: `llama3.3:70b`, `qwen2.5:72b`, `deepseek-r1:70b`.
  - `google_translate_free` — also in this group; covered in detail below.

All of these share the global `_translator_lock`, the `BACKOFF_SCHEDULE` from
`base.py`, and the same delimited-envelope output shape, so they don't get
their own tuning-knob tables in this doc. The only env vars they read are the
secret named by the catalog's `secret_ref_hint` (e.g. `OPENAI_API_KEY`,
`OPENROUTER_API_KEY`, `XAI_API_KEY`). Add a vendor-specific section here only
if a new tuning knob materializes.

## Common rule across all backends

Every backend's `max_parallel` is effectively 1 because the `_translator_lock`
in `services/queue.py` is process-global. The attribute exists for API
compatibility but doesn't loosen the lock. Do **not** replace the lock with
a Semaphore — Claude burns the subscription window in parallel, Gemini /
DeepSeek burn tokens.

## google_translate_free (free tier, online NMT)

Google Translate via the `deep-translator` library, which hits Google's public
web Translate endpoint without authentication. No API key, no per-month quota.
Quality is below the LLM backends but well above the older offline OPUS-MT
predecessor; mechanical NMT still can't follow a glossary instruction, ignores
the genre overlay, ignores `style_note`, ignores the previous-chapter tail.
It's positioned as a **rough draft** so users without an LLM provider can
sample the app, and as a **fidelity reference** that the LLM PEMT pass layers
on top of (see below).

No environment variables. The translator's only knob is the per-call chunk
size (4500 chars, conservative under Google's ~5000-char web-endpoint limit),
configured as `_CHUNK_LIMIT` in
`backend/services/translators/google_translate_free.py`.

`GoogleTranslateFreeTranslator` overrides `BaseTranslator.translate_chapter`
rather than implementing `_complete`. The literary prompt assembled by
`build_prompt` is NOT fed to Google — that prompt is genre-aware and
English-instruction based, neither of which an MT model can act on. Instead
the source text is chunked at paragraph boundaries to stay under the per-call
limit, each chunk is sent through `deep_translator.GoogleTranslator.translate`
inside `asyncio.to_thread`, and the outputs are rejoined with `\n\n` between
chunks.

**Source-language mapping**: novel `source_language` → Google language code
via `_lang_for_google` (`zh → zh-CN`, `ja`/`ko` pass through, unknown falls
back to `auto`).

**TOS note**: the unauthenticated web endpoint is in a TOS gray area for
unattended programmatic use. For a single-user local app translating one
chapter at a time the enforcement risk is near zero. Users running burst
jobs (50+ chapters in seconds) may trip per-IP throttling — the worker
surfaces a clean `Google Translate rate-limited` message and the row lands
in `free_draft_status='error'`, retriable from the reader.

**Lock model**: own `FREE_DRAFT_LOCK` in `services/free_draft_queue.py`,
independent of the LLM `_translator_lock` in `services/queue.py`. Free-draft
work and LLM translation can run concurrently for different chapters — they
don't share API rate limits.

### PEMT (LLM post-editing of NMT)

**Disabled by default (2026-05-29):** `PROMPT_INCLUDE_FREE_DRAFT` defaults
`false`. The mechanical reference anchored the prose toward literal phrasing,
which works against novel-quality output, so the `REFERENCE TRANSLATION` block
is off by default. The free-draft generation lane still fills `free_draft_text`
on chapter open; set `PROMPT_INCLUDE_FREE_DRAFT=true` to feed it into the
prompt. The rest of this section describes the feature when enabled.

When a chapter has both a free draft AND an LLM provider configured, the LLM
prompt picks up a `REFERENCE TRANSLATION` section assembled by
`build_prompt(..., free_draft=...)`. The instruction tells the LLM the draft
is a fidelity anchor (preserve event order / named entities / quantities)
but to write its own natural prose where the draft is awkward. Explicit
`DO NOT TRANSLATE OR COPY VERBATIM` guards against the LLM echoing NMT
phrasing.

The free-draft text is part of the cached prompt body, so a draft change
invalidates the LLM cache for that chapter automatically. `PROMPT_TEMPLATE_VERSION`
was bumped to `phase3-pemt` at the rollout to force-miss pre-PEMT entries.

**Cost note**: PEMT grows the LLM prompt by roughly the chapter length
(~50%). Worth it for the quality lift when terminology fidelity matters; if
you want to skip the draft for a specific chapter, click the "Refresh free
draft" menu item in the reader's overflow menu and dismiss the result.
