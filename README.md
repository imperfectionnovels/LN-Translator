# LN-Translator

Local single-user app that translates foreign-language light novels into English. Paste text, upload a single `.txt` / `.docx` / `.epub` / `.html` file (or many `.txt` files in bulk, one chapter each), or paste a public URL. The backend parses chapters, translates each one through a user-selected AI provider, auto-builds a per-novel glossary so terminology stays consistent, and serves a browser reader with bilingual side-by-side mode. Runs as a Windows desktop app or as a local web server.

## Download (Windows beta-1)

1. Go to the [Releases page](https://github.com/ImperfectionNovels/LN-Translator/releases) and download the latest `LN-Translator-*-windows-x64.zip`.
2. Unzip anywhere. The bundle is self-contained; user data goes to `%APPDATA%\LN-Translator\`.
3. Double-click `LN-Translator.exe`.

The EXE is unsigned for the beta. Windows SmartScreen will show "Windows protected your PC" on first launch. Click **More info**, then **Run anyway**. (Code signing is on the post-beta roadmap.)

## First-run walkthrough

1. The welcome wizard at `/onboarding` opens automatically.
2. Pick a translator provider. The dropdown groups them three ways:
   - **Subscription** (no API key, log in via the vendor's own CLI): **Claude Agent SDK** (recommended quality, install [Claude Code](https://docs.claude.com/claude-code) and run `claude login`), **Claude CLI** (same subscription, subprocess flavor), **OpenAI Codex CLI** (ChatGPT Plus/Pro/Team via `npm i -g @openai/codex` + `codex login`), **Gemini CLI** (Google account via `npm i -g @google/gemini-cli`), and **OpenCode** (multi-provider router covering Anthropic / OpenAI / Google / GitHub Copilot via `opencode auth login`).
   - **API key** (paste a key, stored in the OS credential store): Anthropic, OpenAI, Google Gemini, DeepSeek, xAI Grok, Mistral, OpenRouter (aggregator), Alibaba Qwen, Zhipu GLM, Moonshot Kimi, Groq, or any other OpenAI-compatible vendor via a generic Base-URL entry. The key is stored in the Windows Credential Manager (or the macOS Keychain / Secret Service on those platforms), never written to disk in plaintext, never sent anywhere except the provider you picked.
   - **Local / free** (no API key needed): **Ollama** (talks to a local Ollama server at `http://localhost:11434`, fully offline) and **Google Translate free tier** (online, via the `deep-translator` library hitting Google's public web Translate endpoint, no key, no per-month quota). Lower quality than the LLM providers, but no API key to set up.
3. Paste a chapter or upload a file. Click Translate. The reader opens when the chapter is done.

## What it does

- **Per-novel AI provider selection.** Each novel carries its own translator + optional refinement provider, set from the novel's overview page or the Add Novel dialog on the library card. Refinement runs a second LLM over each draft chapter for surface polish before the reader sees it. The Settings page sets the defaults that new novels start with; existing novels keep what they were configured with.
- **Genre-aware prompts.** Ten genres ship: xianxia, wuxia, modern-romance, isekai, slice-of-life, mystery, litrpg, sci-fi, fantasy, yuri/BL. The system prompt is composed from a base layer plus a per-genre overlay plus worked examples; pick the right genre on the novel page or accept the default.
- **Three import paths.** Paste raw text, upload a single file (`.txt`, `.docx`, `.epub`, `.html`) or many `.txt` files in bulk (one chapter per file), or paste a public URL and let the scraper extract and queue the chapter. EPUB imports also pull the embedded cover.
- **Per-novel glossary.** Auto-extraction admits a term when it appears inside a `【...】` system-interface span or recurs at least twice in the chapter body. Manual edits lock a row against future auto-overwrites. Click any highlighted term in the reader to open the **inline term editor** and rename it across every chapter and chapter title in that novel. "Retranslate affected chapters" replays prior chapters when a term changes mid-novel.
- **Mechanical NMT reference draft for fidelity (off by default).** Optionally, opening a chapter queues a Google-Translate mechanical draft in the background on its own lane (independent of the LLM queue, so the two run in parallel). When the feature is enabled (`PROMPT_INCLUDE_FREE_DRAFT=true`), the Translate call sees that draft as a `REFERENCE TRANSLATION` block and uses it as a fidelity anchor (event order, named entities, quantities) while writing its own natural prose on top. It is disabled by default, because anchoring on a mechanical draft pulls the prose toward literal phrasing; turn it on when terminology fidelity matters more than voice. Requires internet.
- **Reader.** Two modes per session. A clean **read mode** for normal reading, and an **edit mode** that exposes per-paragraph editing, the glossary inspector, and a forced bilingual layout (your edits are captured as future style guidance for the translator). Chapter navigation, bilingual toggle, and dark mode work in both.
- **Downloads.** Plain `.txt`, `.md`, or `.epub` per novel.

## Data and privacy

- The app runs entirely on your machine. Novels, chapters, glossaries, and translations live in a SQLite database under `%APPDATA%\LN-Translator\`.
- API keys go into the OS credential store (Windows Credential Manager / macOS Keychain / Secret Service), never to disk in plaintext, never logged.
- The only outbound network calls are to the AI provider you configured (for translation) and to Google Translate's public web endpoint (for the mechanical NMT reference draft, if enabled). No telemetry, no analytics, no account.
- The downloaded EXE bundle contains no user data of any kind. Your library lives under `%APPDATA%\LN-Translator\` on your own machine and is never bundled, shared, or uploaded.

### Testing as a fresh user

If you want to run the app as if it were a brand-new install (e.g. to verify onboarding or share screenshots without exposing your library), point `LN_TRANSLATOR_DATA` at an empty directory before launching:

```powershell
$env:LN_TRANSLATOR_DATA = "$env:TEMP\ln-translator-fresh"
.\LN-Translator.exe
```

The app uses that directory instead of `%APPDATA%\LN-Translator\` for that session. Your real library is untouched. To return to your real library, close the EXE and unset the variable (`Remove-Item Env:LN_TRANSLATOR_DATA`).

## For developers

The same codebase runs as a local Uvicorn server. Requires Python 3.11+.

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
