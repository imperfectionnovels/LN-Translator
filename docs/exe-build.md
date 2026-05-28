# Building the LN-Translator desktop EXE

This doc describes building, smoke-testing, and distributing the
double-clickable LN-Translator binary on Windows. Same approach works
on macOS / Linux with the obvious platform substitutions, but the
default-target platform is Windows because that's where the user runs.

## Quick start (testers)

1. Download the latest `LN-Translator-*-windows-x64.zip` from the
   [Releases page](https://github.com/ImperfectionNovels/LN-Translator/releases).
2. Unzip anywhere (Desktop, Documents, a USB drive — the bundle is
   self-contained and writes user data to `%APPDATA%\LN-Translator\`).
3. Double-click `LN-Translator.exe`. The first launch opens the
   welcome wizard at `/onboarding`.
4. **Windows SmartScreen warning**: the EXE is unsigned. Windows will
   show "Windows protected your PC" the first time. Click **More info**,
   then **Run anyway**.
5. **Logs**: if something goes wrong on first launch, the diagnostic
   trail lives at `%APPDATA%\LN-Translator\logs\startup.log`. The
   Settings page also has a `Copy Diagnostics` button that bundles the
   same info for bug reports.
6. **Uninstall**: delete the extracted EXE folder, and delete
   `%APPDATA%\LN-Translator\` to remove user data (novels DB, glossary,
   cached translations).
7. **Test as a fresh user**: to run the app with an empty library
   without touching your real data, set
   `$env:LN_TRANSLATOR_DATA = "$env:TEMP\ln-translator-fresh"` before
   launching. The EXE uses that directory instead of
   `%APPDATA%\LN-Translator\` for that session.

## What the EXE does

- Picks a free localhost port starting at `8765`.
- Starts the FastAPI server in a background thread bound to `127.0.0.1`
  (loopback only — never the LAN).
- Waits for `/api/health`, then **opens a native window (pywebview /
  WebView2)** pointed at the local app. **First-run users land on
  `/onboarding`** (the welcome wizard); returning users land on `/`.
  See "First-run welcome wizard" below for the gating logic. The
  console window is hidden in the frozen build (`console=False` in the
  spec), so the user sees only the app window — no DOS-style black box
  behind it.
- Closing the window triggers clean shutdown: pywebview's `closing`
  event sets the shared shutdown flag, uvicorn drains in-flight queue
  workers, lifespan teardown runs, the process exits.
- `Ctrl+C` (when run from a dev terminal, or in the frozen build if
  launched from a console) is also wired into the same shutdown
  funnel. So is the Win32 console-close handler for the rare case
  where someone runs the frozen build with `console=True` overridden
  in their own spec.
- Any chapter mid-translation when the window is closed gets left in
  `'translating'` state and auto-recovers to `'pending'` on the next
  launch via `drain_on_startup`. No data is lost; the user just
  re-clicks Translate.

### Operating modes

The entry point (`backend/app_entry.py`) supports three UI modes:

| Mode | When | Behavior |
| --- | --- | --- |
| **Native window** (default) | pywebview installed AND WebView2 runtime present | Single window owns the main thread. No browser. |
| **Explicit headless** | env `LN_TRANSLATOR_NO_WINDOW=1` | Server boots; no window, no browser tab. Used by the smoke scripts; useful for any other tool that wants to drive the local HTTP server itself. |
| **Degraded fallback** | pywebview import fails (e.g., missing WebView2 runtime) | Falls back to `webbrowser.open()` so the user still has a clickable surface. Logged as a warning to `startup.log`. |

### First-run welcome wizard

`app_entry.py` decides first-run routing by reading
`config_kv.first_run_complete` from the SQLite DB:

| Key state | Routing |
| --- | --- |
| Key missing (clean install)        | `/onboarding` |
| `value = '0'` (user exited mid-wizard) | `/onboarding` |
| `value = '1'` (wizard completed)   | `/`           |

The wizard itself lives at `frontend/onboarding.html` + `frontend/js/onboarding.js`.
It walks the user through:

1. Picking a provider type from the catalog dropdown (19 types, sourced from `backend/services/translator_catalog.py`). The dropdown groups them as Subscription (Claude Agent SDK / Claude CLI / Codex CLI / Gemini CLI / OpenCode), API key (Anthropic / OpenAI / Gemini / DeepSeek / xAI / Mistral / OpenRouter / Qwen / Zhipu / Moonshot / Groq / generic OpenAI-compatible), and Local / Free (Ollama / Google Translate free).
2. Naming the provider and entering a `model_id`.
3. Pasting the API key — stored in the OS keychain via
   `POST /api/providers/{id}/secret` (Windows Credential Manager / macOS
   Keychain / Secret Service), never written to disk in plaintext.
4. Stamping `first_run_complete='1'` via
   `PUT /api/config/first_run_complete`.

The `config_kv` table is reserved for app-level state (it's a small
key/value store managed by `backend/routes/config_kv.py`). Per-novel
state belongs on the `novels` table.

To **re-trigger** the wizard during testing, blow away the appdata dir
(`Remove-Item -Recurse -Force "$env:APPDATA\LN-Translator"`) or use the
`LN_TRANSLATOR_DATA` override (see below) to point at a scratch dir.

### `LN_TRANSLATOR_DATA` override

`backend/config.py` resolves `USER_DATA_ROOT` (where the SQLite DB,
`llm_cache/`, `covers/`, `logs/`, `runtime/`, etc. live) in this order:

1. `LN_TRANSLATOR_DATA` env var, if set — wins everything.
2. `%APPDATA%\LN-Translator\` on Windows / `~/Library/Application Support/LN-Translator/`
   on macOS / XDG path on Linux, when `sys.frozen` is true.
3. `<repo-root>/data/` in dev.

Useful for:

- Driving the EXE against a scratch dir during smoke tests
  (`scripts/smoke-exe.ps1` sets this).
- Pointing at an external drive for users with limited C: space.
- Running two parallel installs side by side without DB collisions.

The override is read **once** at startup; restart the process after
changing it.

### Startup log

Because `console=False` swallows stdout/stderr from the frozen build,
startup-time diagnostics (uncaught exception, health-poll timeout,
port-find failure, pywebview unavailable) are mirrored to:

```
%APPDATA%\LN-Translator\logs\startup.log
```

Size-rotated at 1MB, one rolled file kept. If the EXE seems not to
launch, this is the first file to check.

DB and runtime state live under `%APPDATA%\LN-Translator\` on Windows,
NOT inside the bundle directory. Reinstall / re-extract does not wipe
the user's novels.

## Prerequisites

- Windows 10 or 11 (build on the platform you're targeting).
- Python 3.11 or newer matching the dependencies in `pyproject.toml`.
- A C++ build environment if any transitive dep needs compilation
  (PyInstaller and its hooks usually handle this; if you hit a missing
  `cl.exe`, install the VS Build Tools "Desktop development with C++"
  workload).
- **WebView2 Evergreen Runtime** (for the native window).
  - Windows 11: shipped with the OS, nothing to install.
  - Windows 10: most up-to-date installs already have it via Windows
    Update; older / locked-down systems may need Microsoft's
    [WebView2 Runtime installer](https://developer.microsoft.com/en-us/microsoft-edge/webview2/)
    (free, ~150MB system-side, one-time). The EXE *runs* without it
    — it falls back to opening a browser tab and writes a warning to
    `startup.log` — but the native-window experience requires it.

## Build steps

```powershell
# From the repo root, in an activated venv:
python -m pip install -e .[build]
pyinstaller LN-Translator.spec
```

The build produces `dist\LN-Translator\` containing `LN-Translator.exe`
plus a folder of bundled Python / DLL / data files. The whole folder
must ship together — one-file mode is deliberately NOT used (slower
cold start, higher antivirus false-positive rate, harder to swap a
prompt file without rebuilding).

Build size is typically 80–120 MB after compression depending on which
transitive deps end up vendored.

## Smoke test

For a one-shot scripted check that builds, launches, probes `/api/health`,
verifies the clean-profile state, and tears down — run:

```powershell
.\scripts\smoke-exe.ps1
```

Expected last line: `SMOKE PASS`. The script uses a fresh temp data dir
(`LN_TRANSLATOR_DATA` env override) so it cannot stomp on a real install.
If it fails it prints the tail of the EXE's stdout log to help diagnose.

For an interactive walk-through:

```powershell
.\dist\LN-Translator\LN-Translator.exe
```

Expected:

1. A native window titled **LN-Translator** opens (no console behind
   it, no Chrome tab). On a clean profile it lands on `/onboarding`
   (the welcome wizard); on any subsequent run after the wizard has
   stamped `config_kv.first_run_complete='1'` it lands on `/`.
2. Add a provider → set API key → import a chapter → translate. Output
   must be identical to running from source.
3. Closing the window initiates clean shutdown — uvicorn drains
   in-flight queue workers, lifespan teardown runs, process exits.

To run the EXE without a window (e.g., to drive it from another
script over HTTP):

```powershell
$env:LN_TRANSLATOR_NO_WINDOW = "1"
.\dist\LN-Translator\LN-Translator.exe
# remember to: Remove-Item Env:LN_TRANSLATOR_NO_WINDOW
```

The two smoke scripts (`smoke-exe.ps1` and `smoke_initiative7.py`)
set this env var only inside the child process, so they never leak
into the parent shell.

Verify the appdata story:

```powershell
# Delete appdata to simulate a fresh install:
Remove-Item -Recurse -Force "$env:APPDATA\LN-Translator"

# Re-launch:
.\dist\LN-Translator\LN-Translator.exe

# init_db recreates the schema cleanly; browser lands on /onboarding.
```

Verify port collision:

```powershell
# Hold 8765 in another shell:
python -c "import socket; s=socket.socket(); s.bind(('127.0.0.1',8765)); input()"

# Launch the EXE:
.\dist\LN-Translator\LN-Translator.exe
# Should pick 8766 and open the browser there.
```

Verify paths with spaces:

```powershell
# Copy the build to a path with spaces and re-run.
Copy-Item -Recurse dist\LN-Translator "C:\My Apps\LN-Translator"
& "C:\My Apps\LN-Translator\LN-Translator.exe"
```

## Distribution

Default distribution is a zipped one-folder bundle:

```powershell
Compress-Archive -Path dist\LN-Translator -DestinationPath LN-Translator-windows.zip
```

User unzips anywhere, double-clicks `LN-Translator.exe`. No installer,
no admin privileges, no registry writes. Uninstall = delete the folder.

A polished installer (NSIS or Inno Setup) is the next step if the EXE
ships beyond power users; tracked but not built.

## Known wrinkles

- **`claude_agent_sdk` provider type** depends on a local `claude` CLI
  binary. The frozen EXE doesn't bundle the CLI — users wanting the
  Claude Agent SDK backend must have Claude Code installed separately.
  Documented in the settings UI; the provider "Test" button surfaces a
  clear error if the CLI isn't on PATH.
- **Antivirus false positives**: a brand-new unsigned EXE can trip
  heuristic AV. Code signing is the only long-term fix. Until then,
  document that users may need to allow-list once.
- **First launch is slower** than subsequent: the bundle directory is
  cold in the disk cache and Python imports incur fault-in latency.
  Subsequent launches typically open the browser in under 2s.

## Known build issues (verified 2026-05-23)

These came up while running `scripts/smoke-exe.ps1` for the first time
on Windows 11 + Python 3.14 + PyInstaller 6.20.

- **`charset_normalizer` mypyc hidden-import warnings (benign).** Build
  prints `WARNING: Hidden import "ascii__mypyc" not found!` plus nine
  sibling `*__mypyc` warnings. These are the mypyc-compiled fast-path
  modules that charset_normalizer ships only for some Python versions;
  the pure-Python fallback is bundled and works fine. Safe to ignore.
- **`pycparser` lextab/yacctab warnings (benign).** `pycparser.lextab` /
  `pycparser.yacctab` print as missing hidden imports; pycparser
  generates these at import time when absent, so the bundle works.
- **PyInstaller writes INFO lines to stderr, not stdout.** Anything
  that wraps `pyinstaller` with `2>&1` in Windows PowerShell 5.1 will
  see every INFO line wrap as a `NativeCommandError` ErrorRecord, and
  if `$ErrorActionPreference = "Stop"` is set the build will appear to
  fail at the first INFO line. Don't merge streams; check
  `$LASTEXITCODE` after each native call. (This bit `smoke-exe.ps1`
  during development — fixed by removing the `Stop` preference.)
- **`Invoke-WebRequest` against a refused port takes ~1s on Windows.**
  The `-TimeoutSec` parameter does not apply to TCP connection refusal —
  the .NET stack retries internally and returns after ~1s. Scanning N
  ports HTTP-only takes N seconds. `smoke-exe.ps1` works around this
  with `System.Net.Sockets.TcpClient.ConnectAsync` to find the bound
  port first (sub-millisecond per port) and only HTTP-probes that one.
- **EXE never reaches `/api/health` despite seeming to launch.** Almost
  always the smoke harness's HTTP probe timing out — see the
  `Invoke-WebRequest` bullet above. The actual EXE startup time from
  process-start to `/api/health` returning 200 is consistently under
  1s on Windows 11 with the bundle already in the disk cache.

## Baseline timings (Windows 11, Python 3.14, NVMe SSD)

| Stage                                              | Duration |
| -------------------------------------------------- | -------- |
| `pip install -e .[build]` (already installed)      | ~3s      |
| `pyinstaller ... --clean` (cold)                   | ~48s     |
| `pyinstaller ...` incremental rebuild              | ~42s     |
| EXE process start → `/api/health` returns 200      | <1s      |
| `scripts/smoke-exe.ps1` end-to-end (cold build)    | ~105s    |
| `scripts/smoke-exe.ps1` end-to-end (warm build)    | ~55s     |

## Provider-type compatibility checklist

The EXE bundle ships every backend module in
`backend/services/translator_catalog.py::_CATALOG` (commit `1b61112` switched
the spec to a hard-coded list after discovering `collect_submodules` returned
empty during PyInstaller spec evaluation). Compatibility now splits cleanly by
the catalog's `group` field:

| Group | provider_types | EXE bundle behavior |
| --- | --- | --- |
| Subscription | `claude_agent`, `claude_cli`, `codex_cli`, `gemini_cli`, `opencode` | Backend code is bundled, but auth requires the vendor's own CLI installed separately and logged in (`claude login`, `codex login`, `gemini`, `opencode auth login`). The provider "Test" button surfaces a clear error if the CLI is missing or unauthenticated. |
| API key | `anthropic_api`, `gemini`, `openai`, `deepseek`, `xai`, `mistral`, `openrouter`, `qwen`, `zhipu`, `moonshot`, `groq`, `openai_compatible` | Works out of the box. User pastes the key in the wizard; it goes to the OS keychain (env-var fallback for headless / dev). The catalog's `secret_ref_hint` names the canonical env var (e.g. `OPENAI_API_KEY`, `OPENROUTER_API_KEY`). |
| Local / Free | `ollama`, `google_translate_free` | No external account needed. `ollama` requires a running local Ollama server and a pre-pulled model (fully offline). `google_translate_free` hits Google's public web Translate endpoint via the `deep-translator` library — no key, no quota, but requires internet. |

For users who only have API keys (any vendor in the API-key row), the EXE
works standalone. For users who want a subscription-CLI backend, they install
the vendor's CLI first and the EXE finds it via the system PATH.
