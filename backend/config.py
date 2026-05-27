import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


# Phase 6: detect PyInstaller frozen-bundle mode so file paths point at the
# right places. Two paths matter and they diverge:
#   - BUNDLED_ROOT: where read-only resources live (backend/prompts/*.md,
#     frontend/static/*). PyInstaller extracts these to a temp dir whose
#     path lives in sys._MEIPASS at runtime.
#   - USER_DATA_ROOT: where mutable runtime state lives (data/novels.db,
#     llm_cache, runtime files). Must persist across reinstalls and never
#     get clobbered by a fresh extract, so this lives in %APPDATA% on
#     Windows (and ~/.local/share on Linux, ~/Library/Application Support
#     on macOS via os.environ heuristics).
#
# Dev mode (sys.frozen is False): both point at the repo root, so editing
# a prompt file picks up immediately on the next translation call and the
# DB lives in repo/data/ — matching every iteration before Phase 6.
IS_FROZEN = bool(getattr(sys, "frozen", False))


def _user_data_root() -> Path:
    """Per-user mutable data dir. Honors LN_TRANSLATOR_DATA env var first;
    otherwise picks the platform-appropriate appdata location.

    Layout under this root mirrors the repo's `data/`:
      <root>/novels.db
      <root>/llm_cache/
      <root>/runtime/
    """
    override = os.getenv("LN_TRANSLATOR_DATA")
    if override:
        return Path(override).expanduser()
    if sys.platform.startswith("win"):
        base = os.getenv("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "LN-Translator"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "LN-Translator"
    # Linux / other: XDG_DATA_HOME or ~/.local/share.
    xdg = os.getenv("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "ln-translator"

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    logger.warning(
        "env %s=%r is not a recognised boolean — using default %s",
        name, raw, default,
    )
    return default


if IS_FROZEN:
    # PyInstaller extracts bundled resources to sys._MEIPASS per launch.
    # backend/prompts/base.md ends up at <_MEIPASS>/backend/prompts/base.md
    # because the spec file copies backend/ into the bundle root.
    PROJECT_ROOT = Path(getattr(sys, "_MEIPASS", str(Path(__file__).resolve().parent.parent)))
else:
    # Dev: walk up from this file to the repo root.
    PROJECT_ROOT = Path(__file__).resolve().parent.parent

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_TRANSLATOR_MODEL = os.getenv("GEMINI_TRANSLATOR_MODEL", "gemini-3-pro-preview").strip()
GEMINI_REQUEST_TIMEOUT = _float_env("GEMINI_REQUEST_TIMEOUT", 240.0)

# Default switched to claude_agent (Opus 4.7 with extended thinking) per the
# single-pass restructure: the model deliberates inside one call instead of
# relying on a second humanizer polish pass. claude_cli stays as an option
# but has no thinking-config surface.
TRANSLATOR_BACKEND = os.getenv("TRANSLATOR_BACKEND", "claude_agent").strip().lower()

CLAUDE_CLI_PATH = os.getenv("CLAUDE_CLI_PATH", "claude").strip()
CLAUDE_CLI_TRANSLATOR_MODEL = os.getenv("CLAUDE_CLI_TRANSLATOR_MODEL", "claude-opus-4-5").strip()

CLAUDE_AGENT_TRANSLATOR_MODEL = os.getenv("CLAUDE_AGENT_TRANSLATOR_MODEL", "claude-opus-4-7").strip()
# Per-call timeout (seconds) for the Claude Agent SDK. A genuine long-chapter
# Opus translation with extended thinking finishes inside 8 minutes; a longer
# wait means a hung SDK call, not progress.
CLAUDE_AGENT_CALL_TIMEOUT = _float_env("CLAUDE_AGENT_CALL_TIMEOUT", 600.0)
# Thinking-effort level for the Claude Agent SDK translator: low / medium /
# high / xhigh / max. "high" enables Opus 4.7 extended thinking on the
# translation pass — the lever for noticing-during-writing that the
# single-pass thesis depends on. Blank → omit the option entirely and let
# the SDK pick its default.
CLAUDE_AGENT_TRANSLATOR_EFFORT = os.getenv(
    "CLAUDE_AGENT_TRANSLATOR_EFFORT", "high"
).strip().lower()
if CLAUDE_AGENT_TRANSLATOR_EFFORT not in ("", "low", "medium", "high", "xhigh", "max"):
    CLAUDE_AGENT_TRANSLATOR_EFFORT = "high"

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_TRANSLATOR_MODEL = os.getenv("DEEPSEEK_TRANSLATOR_MODEL", "deepseek-v4-pro").strip()
DEEPSEEK_DRAFT_MODEL = (
    os.getenv("DEEPSEEK_DRAFT_MODEL", "deepseek-chat").strip()
    or DEEPSEEK_TRANSLATOR_MODEL
)
DEEPSEEK_TRANSLATOR_TEMPERATURE = _float_env("DEEPSEEK_TRANSLATOR_TEMPERATURE", 0.7)
DEEPSEEK_REVISION_ENABLED = _bool_env("DEEPSEEK_REVISION_ENABLED", True)
DEEPSEEK_MAX_OUTPUT_TOKENS = _int_env("DEEPSEEK_MAX_OUTPUT_TOKENS", 8192)
DEEPSEEK_REQUEST_TIMEOUT = _float_env("DEEPSEEK_REQUEST_TIMEOUT", 240.0)
DEEPSEEK_REVISION_MODE = os.getenv("DEEPSEEK_REVISION_MODE", "single").strip().lower()
if DEEPSEEK_REVISION_MODE not in ("single", "reflect_improve"):
    DEEPSEEK_REVISION_MODE = "single"

# When true, the translator sees the previous chapter's final 3-5 paragraphs
# (English) as a tone reference for cross-chapter continuity. Opt-out for
# users who hit scene changes / time skips. The block is labelled "DO NOT
# TRANSLATE" in the prompt.
PREVIOUS_CONTEXT_ENABLED = _bool_env("PREVIOUS_CONTEXT_ENABLED", True)
PREVIOUS_CONTEXT_PARAGRAPHS = _int_env("PREVIOUS_CONTEXT_PARAGRAPHS", 4)
PREVIOUS_CONTEXT_MAX_GAP = _int_env("PREVIOUS_CONTEXT_MAX_GAP", 10)

# Hard cap on how many LLM completion calls one chapter is allowed to
# make at the BaseTranslator level (counted at _complete + _complete_plain
# call sites, NOT inside an SDK's own transient-retry loop). Belt-and-
# suspenders over the existing 2-parse-attempt + 1-fallback structure.
# 4 leaves a slot for a future failure mode while still catching any
# regression that turns the retry path into a loop.
MAX_LLM_CALLS_PER_CHAPTER = _int_env("MAX_LLM_CALLS_PER_CHAPTER", 4)

# Default genre key used when a novel has NULL `genre` (and the bootstrap
# seed for the first provider row needs a starting value). Must be a key in
# backend.genres.GENRES; falls back to 'generic' if unknown. Validated lazily
# (not at config-import time) to keep config decoupled from backend.genres.
DEFAULT_GENRE = os.getenv("DEFAULT_GENRE", "xianxia").strip().lower() or "xianxia"

# Known legacy backend names. Kept as a hint for the bootstrap seed function
# (services/providers.py::ensure_default_provider) which translates the
# pre-provider TRANSLATOR_BACKEND env var into a row in the new `providers`
# table on first startup. After seeding, the `providers` table is the source
# of truth — this constant is not used at runtime for routing.
_KNOWN_BACKEND_HINTS = ("claude_cli", "claude_agent", "gemini", "deepseek")
if TRANSLATOR_BACKEND not in _KNOWN_BACKEND_HINTS:
    logger.warning(
        "TRANSLATOR_BACKEND=%r is not in the known-backend hint list %s; "
        "bootstrap seed will skip creating a default Provider from env. "
        "Configure providers via the settings UI instead.",
        TRANSLATOR_BACKEND, _KNOWN_BACKEND_HINTS,
    )

# USER_DATA_ROOT is the directory mutable runtime state lives in. Dev mode
# defaults to repo/data/; frozen mode defaults to %APPDATA%/LN-Translator (or
# platform equivalent). LN_TRANSLATOR_DATA overrides BOTH defaults so a
# developer can run `LN_TRANSLATOR_DATA=/tmp/scratch python -m backend.app_entry`
# without polluting the real reading library. Other modules (llm_cache,
# claude_agent's runtime prompt files) read this to land their writable
# state in the right place — they must NOT write under PROJECT_ROOT in
# frozen mode because that's sys._MEIPASS, read-only and per-launch ephemeral.
_user_data_override = os.getenv("LN_TRANSLATOR_DATA")
if _user_data_override:
    USER_DATA_ROOT = Path(_user_data_override).expanduser()
elif IS_FROZEN:
    USER_DATA_ROOT = _user_data_root()
else:
    USER_DATA_ROOT = PROJECT_ROOT / "data"

_db_env = os.getenv("DB_PATH")
if _db_env:
    DB_PATH = Path(_db_env)
else:
    DB_PATH = USER_DATA_ROOT / "novels.db"
FRONTEND_DIR = PROJECT_ROOT / "frontend"

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
USER_DATA_ROOT.mkdir(parents=True, exist_ok=True)
