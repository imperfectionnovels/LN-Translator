# PyInstaller spec for the LN-Translator desktop EXE.
#
# Build:
#   python -m pip install -e .[build]
#   pyinstaller LN-Translator.spec
#
# Output: dist/LN-Translator/LN-Translator.exe (Windows) + a folder of
# bundled dependencies. Zip the whole dist/LN-Translator/ folder for
# distribution. One-folder mode (NOT --onefile) because:
#   - faster cold start (no per-launch extract to temp dir)
#   - lower antivirus false-positive rate
#   - easier to swap a bundled .md prompt file without rebuilding
#
# Build environment notes:
#   - Run on Windows for a Windows build, macOS for macOS, Linux for Linux.
#     PyInstaller cross-compilation is not supported.
#   - claude_agent_sdk shells out to the local `claude` CLI. The frozen
#     EXE bundle does NOT include the CLI itself — users who want the
#     claude_agent (or claude_cli) provider type must have Claude Code
#     installed separately. The settings UI surfaces a clear error if
#     the CLI isn't on PATH. See docs/exe-build.md "Provider-type
#     compatibility checklist".

# ruff: noqa  -- this file is parsed by PyInstaller, not Python tooling.

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

# Bundle the prompts hierarchy (read at runtime by build_system_instruction)
# and the frontend statics (served by FastAPI's mount). PyInstaller copies
# these into the bundle root; backend.config detects sys.frozen and resolves
# PROJECT_ROOT to sys._MEIPASS so the files load from the bundle dir.
datas = [
    ("backend/prompts", "backend/prompts"),
    ("frontend", "frontend"),
]

# Library data that PyInstaller's static analysis often misses:
#   - trafilatura: ships language-detection / boilerplate models.
#   - certifi: the CA bundle used by httpx for TLS verification.
#   - tldextract (transitive via trafilatura): public suffix list snapshot.
#   - ebooklib: minimal data but its ZIP-handling code paths sometimes
#     escape static analysis. (Initiative 7 EPUB importer + exporter.)
#   - docx (python-docx): ships default-template XML under docx/templates/
#     and oxml schema fragments. (Initiative 7 DOCX importer.)
datas += collect_data_files("trafilatura")
datas += collect_data_files("certifi")
try:
    datas += collect_data_files("tldextract")
except Exception:
    pass
datas += collect_data_files("ebooklib")
datas += collect_data_files("docx")
# zhconv ships its simplified/traditional dict as zhcdict.json sitting
# alongside the package's .py files. PyInstaller's static analysis
# doesn't pick up the dict because it's loaded via importlib.resources
# at first call (inside glossary_filters._zh_convert) — without it the
# translator worker crashes during the glossary-merge step and the
# whole Translate flow fails with `[Errno 2] No such file or directory`.
datas += collect_data_files("zhconv")

# Hidden imports: modules imported via string (importlib, __import__),
# stringly-typed entry points, or dynamically resolved by frozen-mode
# entry points. The translator factory dispatches to backends by
# provider_type name (factory.py::_DISPATCH → importlib.import_module),
# so EVERY backend in backend.services.translators must ship even when
# nothing statically references it from app_entry. Listed explicitly
# because collect_submodules("backend.services.translators") returns an
# empty list during spec evaluation in this build env — the manual list
# is the only path that reliably bundles all 19 backends. Keep this in
# sync with factory.py::_DISPATCH when a new translator lands; the
# catalog-parity test catches drift between catalog/dispatch but does
# not see this spec file.
hiddenimports = [
    "backend.services.free_draft_queue",
    # Subscription / CLI subprocess backends.
    "backend.services.translators.claude_agent",
    "backend.services.translators.claude_cli",
    "backend.services.translators.codex_cli",
    "backend.services.translators.gemini_cli",
    "backend.services.translators.opencode",
    # API-key backends.
    "backend.services.translators.anthropic_api",
    "backend.services.translators.gemini",
    "backend.services.translators.openai",
    "backend.services.translators.deepseek",
    "backend.services.translators.deepseek_revise",
    "backend.services.translators.xai",
    "backend.services.translators.mistral",
    "backend.services.translators.openrouter",
    "backend.services.translators.qwen",
    "backend.services.translators.zhipu",
    "backend.services.translators.moonshot",
    "backend.services.translators.groq",
    "backend.services.translators.openai_compatible",
    "backend.services.translators.openai_compatible_generic",
    # Local.
    "backend.services.translators.ollama",
    "backend.services.translators.google_translate_free",
    # keyring backends — pick whichever module the platform actually has.
    # The frozen build needs at least one or set_secret returns 503.
    "keyring.backends.Windows",
    "keyring.backends.macOS",
    "keyring.backends.SecretService",
]
# Free-tier mechanical NMT backend: deep-translator (pure Python, wraps
# Google's web Translate endpoint). No compiled extensions — collect_submodules
# is enough to bundle the package. The 5K-char chunking and exception handling
# live in backend/services/translators/google_translate_free.py.
hiddenimports += collect_submodules("deep_translator")
binaries_free_draft: list = []
# httpx + trafilatura sometimes import their submodules dynamically.
hiddenimports += collect_submodules("httpx")
hiddenimports += collect_submodules("trafilatura")
# Initiative 7 dependencies: ebooklib walks its own ITEM_* constants and
# python-docx pulls in lxml-backed oxml submodules lazily. Both are
# documented as PyInstaller-edge-case packages.
hiddenimports += collect_submodules("ebooklib")
hiddenimports += collect_submodules("docx")
# pywebview ships its own PyInstaller hook in 5.x+ that handles most
# platform shims, but `collect_submodules("webview")` is a belt-and-
# suspenders catch for webview.platforms.edgechromium and friends — the
# platform-specific backends are picked at import time, not statically
# referenced from app_entry.py.
hiddenimports += collect_submodules("webview")
# Per-site scraper recipes — registered at import time via the
# __init__.py side-effect chain. PyInstaller's static analysis doesn't
# pick up the dynamic register() calls, so we tell it explicitly.
hiddenimports += collect_submodules("backend.services.scrapers")
# cloudscraper has internal modules (interpreters, user_agent, etc.)
# picked at import time depending on the challenge type encountered.
hiddenimports += collect_submodules("cloudscraper")
# curl_cffi: compiled C extension for Chrome TLS impersonation. The
# primary tier of the CF bypass chain. PyInstaller's static analysis
# misses the dynamically-loaded curl-impersonate binaries; collect_data
# brings them along.
hiddenimports += collect_submodules("curl_cffi")
try:
    datas += collect_data_files("curl_cffi")
except Exception:
    pass
# beautifulsoup4 is used by site recipes for CSS-selector extraction.
hiddenimports += collect_submodules("bs4")


a = Analysis(
    ["backend/app_entry.py"],
    pathex=[],
    binaries=binaries_free_draft,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Test framework + dev tools don't need to ship.
        "pytest",
        "pytest_asyncio",
        # torch + transformers + huggingface_hub get pulled transitively
        # via try/except imports inside huggingface-related packages, but
        # this app never calls into them. torch alone is ~360 MB of the
        # bundle (torch_cpu.dll is 293 MB). Excluding here reclaims that.
        "torch",
        "transformers",
        "transformers.models",
        "huggingface_hub",
        # ctranslate2 + sentencepiece: only used by the removed OPUS-MT
        # backend. Excluded so a stray dev install in the build env
        # doesn't pull them into the bundle.
        "ctranslate2",
        "sentencepiece",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    noarchive=False,
    optimize=2,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LN-Translator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,        # UPX trips antivirus false positives more than it saves space
    # console=False: the EXE is a desktop app — uvicorn runs in the background
    # and a pywebview window is the UI surface. A visible console window
    # would defeat the "real native app" feel. Startup diagnostics are
    # mirrored to USER_DATA_ROOT/logs/startup.log so an early failure is
    # still recoverable. See backend/app_entry.py::_install_startup_log_handler.
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LN-Translator",
)
