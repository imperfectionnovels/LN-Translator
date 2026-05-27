# LN-Translator

Local single-user app that translates foreign-language light novels into English. Paste text, upload `.txt` / `.docx` / `.epub` / `.html` files (single or bulk), or paste a public URL. The backend parses chapters, translates each one through a user-selected AI provider, auto-builds a per-novel glossary so terminology stays consistent, and serves a browser reader with bilingual side-by-side mode. Runs as a Windows desktop app or as a local web server.

## Download (Windows beta-1)

1. Go to the [Releases page](https://github.com/ImperfectionNovels/LN-Translator/releases) and download the latest `LN-Translator-*-windows-x64.zip`.
2. Unzip anywhere. The bundle is self-contained; user data goes to `%APPDATA%\LN-Translator\`.
3. Double-click `LN-Translator.exe`.

The EXE is unsigned for the beta. Windows SmartScreen will show "Windows protected your PC" on first launch. Click **More info**, then **Run anyway**. (Code signing is on the post-beta roadmap.)

## First-run walkthrough

1. The welcome wizard at `/onboarding` opens automatically.
2. Pick a translator provider:
   - **Claude Agent SDK** (recommended quality): no API key, but you have to install [Claude Code](https://docs.claude.com/claude-code) separately and run `claude` once to log in. Translations run against your local Claude subscription.
   - **Gemini / DeepSeek / other API providers**: paste an API key. The key is stored in the Windows Credential Manager (or the macOS Keychain / Secret Service on those platforms), never written to disk in plaintext, never sent anywhere except the provider you picked.
   - **OPUS-MT free tier**: no key needed. Triggers a one-time ~200 MB model download per language pair on first use. Lower quality than the LLM providers but fully offline after the download.
3. Paste a chapter or upload a file. Click Translate. The reader opens when the chapter is done.

## What it does

- **Per-novel AI provider selection.** Pick a translator + an optional refinement provider per novel from the Settings page. Refinement runs a second LLM over each draft chapter for surface polish before the reader sees it.
- **Genre-aware prompts.** Ten genres ship: xianxia, wuxia, modern-romance, isekai, slice-of-life, mystery, litrpg, sci-fi, fantasy, yuri/BL. The system prompt is composed from a base layer plus a per-genre overlay plus worked examples; pick the right genre on the novel page or accept the default.
- **Three import paths.** Paste raw text, upload a single file (`.txt`, `.docx`, `.epub`, `.html`) or many `.txt` files in bulk (one chapter per file), or paste a public URL and let the scraper extract and queue the chapter. EPUB imports also pull the embedded cover.
- **Per-novel glossary.** Auto-extraction admits a term when it appears inside a `【...】` system-interface span or recurs at least twice in the chapter body. Manual edits lock a row against future auto-overwrites. "Retranslate affected chapters" replays prior chapters when a term changes mid-novel.
- **Reader.** Two modes per session. A clean **read mode** for normal reading, and an **edit mode** that exposes per-paragraph editing, the glossary inspector, and a forced bilingual layout (your edits are captured as future style guidance for the translator). Chapter navigation, bilingual toggle, and dark mode work in both.
- **Downloads.** Plain `.txt`, `.md`, or `.epub` per novel.

## Data and privacy

- The app runs entirely on your machine. Novels, chapters, glossaries, and translations live in a SQLite database under `%APPDATA%\LN-Translator\`.
- API keys go into the OS credential store (Windows Credential Manager / macOS Keychain / Secret Service), never to disk in plaintext, never logged.
- The only outbound network calls are to the AI provider you configured (for translation) and to GitHub release assets (for the optional OPUS-MT model download). No telemetry, no analytics, no account.
- The downloaded EXE bundle contains no user data of any kind. Your library lives under `%APPDATA%\LN-Translator\` on your own machine and is never bundled, shared, or uploaded.

### Testing as a fresh user

If you want to run the app as if it were a brand-new install (e.g. to verify onboarding or share screenshots without exposing your library), point `LN_TRANSLATOR_DATA` at an empty directory before launching:

```powershell
$env:LN_TRANSLATOR_DATA = "$env:TEMP\ln-translator-fresh"
.\LN-Translator.exe
```

The app uses that directory instead of `%APPDATA%\LN-Translator\` for that session. Your real library is untouched. To return to your real library, close the EXE and unset the variable (`Remove-Item Env:LN_TRANSLATOR_DATA`).

## For developers

The same codebase runs as a local Uvicorn server. Requires Python 3.11+ and a C++ build toolchain for the OPUS-MT native dependencies.

```powershell
pip install -e .
uvicorn backend.main:app --reload --port 8000
# or run the packaged-style entry point (picks a free port from 8765, opens a pywebview window):
python -m backend.app_entry
```

Open <http://localhost:8000> (or the port `app_entry` printed). First run lands on `/onboarding`.

Tests: `pytest backend/tests`. Build the EXE: `pip install -e .[build] && pyinstaller LN-Translator.spec --clean` (see [docs/exe-build.md](docs/exe-build.md) for the full workflow).

## Project docs

- [CLAUDE.md](CLAUDE.md) — architecture orientation for coding agents and contributors.
- [docs/backends.md](docs/backends.md) — per-backend tuning knobs.
- [docs/exe-build.md](docs/exe-build.md) — desktop EXE build + first-run setup.
- [docs/gotchas.md](docs/gotchas.md) — recurring pitfalls the project keeps hitting.

## License

MIT. See [LICENSE](LICENSE).
