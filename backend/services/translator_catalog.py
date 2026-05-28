"""Translator catalog — the single source of truth for supported provider
types, their curated model versions, and the auth/base-URL/secret hints the
Add Provider dialog uses to fill itself in.

Where this is read:
- `services/providers.py::KNOWN_PROVIDER_TYPES` — derived from this catalog,
  so the API allow-list and the UI dropdown can't drift apart.
- `services/translators/factory.py::_DISPATCH` — derived from this catalog,
  so adding a new type is one entry here + one translator class.
- `routes/providers.py::/api/providers/catalog` — exposes the catalog as
  JSON for the Settings dialog + onboarding step to consume.

Adding a new type: add a `TypeEntry(...)` to `_CATALOG` below, drop a
`backend/services/translators/<type>.py` subclass of `BaseTranslator` (or
`openai_compatible.OpenAICompatibleTranslator`), and register the class in
`factory.py::_DISPATCH`. The catalog-parity test enforces both ends agree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# Sentinel model id appended to every type that allows a free-text model.
# The frontend treats this as "Other (custom ID)…" — selecting it reveals
# the free-text Model ID input.
CUSTOM_MODEL_SENTINEL = "__custom__"


@dataclass(frozen=True)
class ModelEntry:
    """One curated model version inside a provider type's catalog."""
    id: str       # passed verbatim to the API as model_id
    display: str  # UI label


@dataclass(frozen=True)
class TypeEntry:
    """One provider type — drives the Type dropdown, auth field visibility,
    and Model dropdown contents."""
    type: str
    display: str
    group: Literal["Subscription", "API key", "Local"]
    auth: Literal["subscription", "api_key", "none"]
    base_url_default: str | None = None
    secret_ref_hint: str | None = None
    # When True, the Model dropdown appends an "Other (custom ID)…" option
    # that reveals a free-text input. ALWAYS True today — a hardcoded list
    # would block users when a new model ships before the catalog updates.
    supports_custom_model: bool = True
    # Optional helper text shown beneath the Type field on the Add Provider
    # form. Use it for install / login hints ("run `codex login` first").
    install_hint: str | None = None
    # For subscription / local-auth types: the shell command the user runs
    # to authenticate, displayed in a copy-friendly box in the Add Provider
    # dialog's auth callout. None for api_key types (their auth happens via
    # the inline API-key field instead).
    auth_command: str | None = None
    models: tuple[ModelEntry, ...] = field(default_factory=tuple)


# ---- Curated model lists ----
#
# Conservative bias: we list models that actually exist as of early 2026.
# When in doubt, leave it out — the "Other (custom ID)…" escape hatch covers
# anything we missed.

_CLAUDE_MODELS = (
    ModelEntry("claude-opus-4-8", "Claude Opus 4.8"),
    ModelEntry("claude-opus-4-7", "Claude Opus 4.7"),
    ModelEntry("claude-opus-4-6", "Claude Opus 4.6"),
    ModelEntry("claude-sonnet-4-6", "Claude Sonnet 4.6"),
    ModelEntry("claude-haiku-4-5-20251001", "Claude Haiku 4.5"),
)

_GEMINI_MODELS = (
    ModelEntry("gemini-3-pro-preview", "Gemini 3 Pro (preview)"),
    ModelEntry("gemini-2.5-pro", "Gemini 2.5 Pro"),
    ModelEntry("gemini-2.5-flash", "Gemini 2.5 Flash"),
    ModelEntry("gemini-2.0-flash", "Gemini 2.0 Flash"),
)

_DEEPSEEK_MODELS = (
    ModelEntry("deepseek-chat", "DeepSeek Chat (V3)"),
    ModelEntry("deepseek-reasoner", "DeepSeek Reasoner (R1)"),
)

_OPENAI_MODELS = (
    ModelEntry("gpt-5", "GPT-5"),
    ModelEntry("gpt-5-mini", "GPT-5 mini"),
    ModelEntry("gpt-4.1", "GPT-4.1"),
    ModelEntry("gpt-4o", "GPT-4o"),
    ModelEntry("o3", "o3"),
    ModelEntry("o3-mini", "o3-mini"),
)

_CODEX_MODELS = (
    ModelEntry("gpt-5.5", "GPT-5.5"),
    ModelEntry("gpt-5", "GPT-5"),
    ModelEntry("gpt-5-codex", "GPT-5 Codex"),
    ModelEntry("o3", "o3"),
)

_XAI_MODELS = (
    ModelEntry("grok-4", "Grok 4"),
    ModelEntry("grok-3", "Grok 3"),
    ModelEntry("grok-3-mini", "Grok 3 mini"),
)

_MISTRAL_MODELS = (
    ModelEntry("mistral-large-latest", "Mistral Large"),
    ModelEntry("mistral-medium-latest", "Mistral Medium"),
    ModelEntry("mistral-small-latest", "Mistral Small"),
)

_QWEN_MODELS = (
    ModelEntry("qwen-max", "Qwen Max"),
    ModelEntry("qwen-plus", "Qwen Plus"),
    ModelEntry("qwen-turbo", "Qwen Turbo"),
)

_ZHIPU_MODELS = (
    ModelEntry("glm-4.6", "GLM-4.6"),
    ModelEntry("glm-4-plus", "GLM-4 Plus"),
    ModelEntry("glm-4-air", "GLM-4 Air"),
)

_MOONSHOT_MODELS = (
    ModelEntry("kimi-k2", "Kimi K2"),
    ModelEntry("kimi-k1.5", "Kimi K1.5"),
)

_GROQ_MODELS = (
    ModelEntry("llama-3.3-70b-versatile", "Llama 3.3 70B Versatile"),
    ModelEntry("llama-3.1-70b-versatile", "Llama 3.1 70B Versatile"),
    ModelEntry("mixtral-8x7b-32768", "Mixtral 8x7B"),
)


# ---- The catalog ----

_CATALOG: tuple[TypeEntry, ...] = (
    # ---------- Subscription (no API key — auth out-of-band) ----------
    TypeEntry(
        type="claude_agent",
        display="Claude Agent SDK (local subscription)",
        group="Subscription",
        auth="subscription",
        install_hint="Uses your local Claude Code subscription via the Agent SDK — no API key needed. Just make sure you're logged into Claude Code.",
        auth_command="claude login",
        models=_CLAUDE_MODELS,
    ),
    TypeEntry(
        type="claude_cli",
        display="Claude CLI (subprocess, subscription)",
        group="Subscription",
        auth="subscription",
        install_hint="Spawns the `claude` CLI as a subprocess. Install Claude Code from https://docs.claude.com/claude-code, then log in once from your terminal.",
        auth_command="claude login",
        models=_CLAUDE_MODELS,
    ),
    TypeEntry(
        type="codex_cli",
        display="OpenAI Codex CLI (ChatGPT subscription)",
        group="Subscription",
        auth="subscription",
        install_hint="Uses your ChatGPT Plus / Pro / Team subscription via the Codex CLI. Install with `npm i -g @openai/codex`, then log in once from your terminal.",
        auth_command="codex login",
        models=_CODEX_MODELS,
    ),
    TypeEntry(
        type="gemini_cli",
        display="Google Gemini CLI (Google account)",
        group="Subscription",
        auth="subscription",
        install_hint="Uses your Google account (free tier + Gemini Advanced) via the Gemini CLI. Install with `npm i -g @google/gemini-cli`, then run `gemini` once and follow the OAuth prompt.",
        auth_command="gemini",
        models=_GEMINI_MODELS,
    ),
    TypeEntry(
        type="opencode",
        display="OpenCode (multi-provider router)",
        group="Subscription",
        auth="subscription",
        install_hint="Routes to whichever providers you've logged into via OpenCode (Anthropic / OpenAI / GitHub Copilot / …). Install from https://opencode.ai, then run `opencode auth login` and pick the providers you want to use. Model IDs use OpenCode's `<provider>/<model>` namespacing.",
        auth_command="opencode auth login",
        models=(
            ModelEntry("anthropic/claude-opus-4-7", "Anthropic · Claude Opus 4.7"),
            ModelEntry("anthropic/claude-sonnet-4-6", "Anthropic · Claude Sonnet 4.6"),
            ModelEntry("openai/gpt-5", "OpenAI · GPT-5"),
            ModelEntry("google/gemini-2.5-pro", "Google · Gemini 2.5 Pro"),
            ModelEntry("github-copilot/gpt-5", "GitHub Copilot · GPT-5"),
        ),
    ),

    # ---------- API key ----------
    TypeEntry(
        type="anthropic_api",
        display="Anthropic Claude (API key)",
        group="API key",
        auth="api_key",
        base_url_default=None,
        secret_ref_hint="ANTHROPIC_API_KEY",
        models=_CLAUDE_MODELS,
    ),
    TypeEntry(
        type="gemini",
        display="Google Gemini (API key)",
        group="API key",
        auth="api_key",
        base_url_default=None,
        secret_ref_hint="GEMINI_API_KEY",
        models=_GEMINI_MODELS,
    ),
    TypeEntry(
        type="openai",
        display="OpenAI (API key)",
        group="API key",
        auth="api_key",
        base_url_default="https://api.openai.com/v1",
        secret_ref_hint="OPENAI_API_KEY",
        models=_OPENAI_MODELS,
    ),
    TypeEntry(
        type="deepseek",
        display="DeepSeek (OpenAI-compatible)",
        group="API key",
        auth="api_key",
        base_url_default="https://api.deepseek.com",
        secret_ref_hint="DEEPSEEK_API_KEY",
        models=_DEEPSEEK_MODELS,
    ),
    TypeEntry(
        type="xai",
        display="xAI Grok (OpenAI-compatible)",
        group="API key",
        auth="api_key",
        base_url_default="https://api.x.ai/v1",
        secret_ref_hint="XAI_API_KEY",
        models=_XAI_MODELS,
    ),
    TypeEntry(
        type="mistral",
        display="Mistral (OpenAI-compatible)",
        group="API key",
        auth="api_key",
        base_url_default="https://api.mistral.ai/v1",
        secret_ref_hint="MISTRAL_API_KEY",
        models=_MISTRAL_MODELS,
    ),
    TypeEntry(
        type="openrouter",
        display="OpenRouter (aggregator)",
        group="API key",
        auth="api_key",
        base_url_default="https://openrouter.ai/api/v1",
        secret_ref_hint="OPENROUTER_API_KEY",
        install_hint="OpenRouter routes to many models under one key. Model IDs use the `provider/model` form (e.g. `anthropic/claude-opus-4`).",
        models=(
            ModelEntry("anthropic/claude-opus-4", "Anthropic · Claude Opus 4"),
            ModelEntry("openai/gpt-5", "OpenAI · GPT-5"),
            ModelEntry("google/gemini-2.5-pro", "Google · Gemini 2.5 Pro"),
            ModelEntry("deepseek/deepseek-chat", "DeepSeek · Chat (V3)"),
            ModelEntry("meta-llama/llama-3.3-70b-instruct", "Meta · Llama 3.3 70B"),
        ),
    ),
    TypeEntry(
        type="qwen",
        display="Alibaba Qwen (OpenAI-compatible)",
        group="API key",
        auth="api_key",
        base_url_default="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        secret_ref_hint="DASHSCOPE_API_KEY",
        models=_QWEN_MODELS,
    ),
    TypeEntry(
        type="zhipu",
        display="Zhipu GLM (OpenAI-compatible)",
        group="API key",
        auth="api_key",
        base_url_default="https://open.bigmodel.cn/api/paas/v4",
        secret_ref_hint="ZHIPU_API_KEY",
        models=_ZHIPU_MODELS,
    ),
    TypeEntry(
        type="moonshot",
        display="Moonshot Kimi (OpenAI-compatible)",
        group="API key",
        auth="api_key",
        base_url_default="https://api.moonshot.cn/v1",
        secret_ref_hint="MOONSHOT_API_KEY",
        models=_MOONSHOT_MODELS,
    ),
    TypeEntry(
        type="groq",
        display="Groq (OpenAI-compatible)",
        group="API key",
        auth="api_key",
        base_url_default="https://api.groq.com/openai/v1",
        secret_ref_hint="GROQ_API_KEY",
        models=_GROQ_MODELS,
    ),
    TypeEntry(
        type="openai_compatible",
        display="Generic OpenAI-compatible",
        group="API key",
        auth="api_key",
        base_url_default=None,
        secret_ref_hint="OPENAI_API_KEY",
        install_hint="For any vendor exposing an OpenAI-compatible /chat/completions endpoint. Set the Base URL yourself.",
        models=(),  # Pure free-text: no curated suggestions.
    ),

    # ---------- Local ----------
    TypeEntry(
        type="ollama",
        display="Ollama (local)",
        group="Local",
        auth="none",
        base_url_default="http://localhost:11434/v1",
        secret_ref_hint=None,
        install_hint="Talks to a local Ollama server (default http://localhost:11434). No API key needed — just install Ollama from https://ollama.com and pull the model first.",
        auth_command="ollama pull <model-name>",
        models=(
            ModelEntry("llama3.3:70b", "Llama 3.3 70B"),
            ModelEntry("qwen2.5:72b", "Qwen 2.5 72B"),
            ModelEntry("deepseek-r1:70b", "DeepSeek R1 70B"),
        ),
    ),
    # Free-tier online MT (Google Translate via deep-translator). No API
    # key, no per-month quota — hits Google's public web Translate endpoint.
    # Source language is taken from the novel, not the provider, so there is
    # only one model id (`google-web`). Used both as a main translator (free
    # rough draft) and as the free-draft engine that the LLM PEMT pass reads
    # back as a fidelity anchor.
    TypeEntry(
        type="google_translate_free",
        display="Free tier (Google Translate, online)",
        group="Local",
        auth="none",
        base_url_default=None,
        secret_ref_hint=None,
        supports_custom_model=False,
        install_hint="Free / online rough draft. No API key. Translation goes through Google's public web endpoint via the `deep-translator` library; quality is below the LLM backends but well above the old offline OPUS-MT free tier.",
        auth_command=None,
        models=(
            ModelEntry("google-web", "Google Translate (web)"),
        ),
    ),
)


def all_types() -> tuple[TypeEntry, ...]:
    """Return every TypeEntry. Frozen tuple — callers can iterate but not
    mutate."""
    return _CATALOG


def all_type_keys() -> frozenset[str]:
    """Set of every known `provider_type` value. Drives the API allow-list
    in services/providers.py."""
    return frozenset(t.type for t in _CATALOG)


def get_type(provider_type: str) -> TypeEntry | None:
    """Return the catalog entry for `provider_type`, or None if unknown."""
    for entry in _CATALOG:
        if entry.type == provider_type:
            return entry
    return None


def to_api_payload() -> list[dict]:
    """Serialize the catalog for the GET /api/providers/catalog endpoint.
    Stable shape so the frontend can render it without per-type special-
    casing."""
    out: list[dict] = []
    for entry in _CATALOG:
        out.append({
            "type": entry.type,
            "display": entry.display,
            "group": entry.group,
            "auth": entry.auth,
            "base_url_default": entry.base_url_default,
            "secret_ref_hint": entry.secret_ref_hint,
            "supports_custom_model": entry.supports_custom_model,
            "install_hint": entry.install_hint,
            "auth_command": entry.auth_command,
            "models": [{"id": m.id, "display": m.display} for m in entry.models],
            "custom_model_sentinel": CUSTOM_MODEL_SENTINEL,
        })
    return out
