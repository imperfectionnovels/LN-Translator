# AGENTS.md

This project's authoritative guidance lives in `CLAUDE.md` — read that first
regardless of which agent you are.

## Current architecture

The app is a multi-provider, per-novel-configurable, packaged desktop
translator. Original target was Chinese xianxia novels; the per-novel
provider + genre design generalizes to any source language and genre
the prompt overlays support.

Key facts:

- **AI providers are generic.** A `Provider` row in the database describes one
  backend with its own `model_id`, `base_url`, and `secret_ref`. Novels reference
  providers by id; the queue worker routes through `provider.provider_type`. The
  authoritative list of supported types lives in
  `backend/services/translator_catalog.py::_CATALOG` (currently 19 types across
  the Subscription / API key / Local groups; the dropdown in the Add Provider
  dialog is generated from it). **Do not hardcode model versions in routing
  logic** — treat provider type + model as data.
- **Per-novel settings** drive each translation: `translator_provider_id`,
  optional `refinement_provider_id` (Phase 4), `genre`, `custom_style_brief`,
  `source_language`. NULL falls back to defaults via `resolve_genre` and the
  `is_default=1` provider row.
- **Genre-aware system instruction** composed per call from
  `backend/prompts/base.md` + `backend/prompts/genres/<genre>.md` +
  `backend/prompts/examples/<genre>.md` via
  `build_system_instruction(genre, custom_brief)` in
  `services/translators/base.py`. 10 genres ship: xianxia, wuxia, modern-romance,
  isekai, slice-of-life, mystery, litrpg, sci-fi, fantasy, yuri-bl. (`generic`
  is the internal fallback for legacy rows, not user-facing.) Custom brief
  appends after the overlay; whitespace-only briefs are normalized to None.
- **Refinement (opt-in, Phase 4 shipped).** A per-novel
  `refinement_provider_id` triggers a second pass over the translator's draft.
  State machine on `chapters` (`refinement_status`: none/pending/in_progress/
  done/error); the reader, downloads, and FTS all prefer `refined_text` once
  `refinement_status='done'`. drain_on_startup resets stuck `in_progress`
  rows so crashed refinements auto-resume.
- **URL scraping (Phase 5 shipped).** `POST /api/translate/scrape` accepts a
  URL and routes the extracted text through the existing parser pipeline.
  Mandatory security guards in `backend/services/scraper.py`: SSRF
  rejection of internal addresses (loopback / RFC1918 / link-local /
  reserved), per-hop manual redirect validation (no httpx auto-follow),
  10 MB response cap, 15s wall-clock timeout via asyncio, Chrome-shaped
  User-Agent + client hints (polite mode behind `LN_TRANSLATOR_POLITE_UA=1`).
  Generic extraction via trafilatura. **Per-site recipes** under
  `backend/services/scrapers/` own the full import end-to-end for hosts
  that need special handling (69shuba.com — GBK encoding, Firefox UA,
  chapter-list crawl with rate-limit throttle). Cloudflare bypass chain
  in `backend/services/scrapers/cloudflare.py`: `curl_cffi` (Chrome TLS
  impersonation) is tried first, `cloudscraper` (legacy JS challenge
  solver) second.
- **Desktop EXE (Phase 6 shipped).** `backend/app_entry.py` is the frozen
  entry point; `LN-Translator.spec` configures a PyInstaller `--onedir`
  bundle. Build: `python -m pip install -e .[build]; pyinstaller
  LN-Translator.spec`. See `docs/exe-build.md` for the full workflow.
  `backend/config.py` resolves `USER_DATA_ROOT` to `%APPDATA%/...` when
  `sys.frozen` is true so the SQLite DB and runtime files survive
  reinstalls. Secrets via `keyring` (Credential Manager / Keychain /
  Secret Service), env-var fallback for headless / dev contexts.
  `POST /api/providers/{id}/set-secret` and DELETE `.../secret` manage
  the keyring entries from the settings UI.
- **Glossary-context observers (Phase 3)** flag prose patterns around locked
  terms: double-possessive pileups, intensifier inflation
  (`detect_intensifier_inflation_on_glossary_term`), predicate loss across
  13 verb groups (`_PREDICATE_GROUPS`), malformed cultivation-noun stacks,
  mid-sentence paragraph breaks. Observers LOG only; the single-pass thesis
  is that noticing has to happen inside the translator's thinking phase.

Subscription-CLI backends (`codex_cli`, `gemini_cli`, `opencode`) reuse the
same shape as `claude_cli`: they shell out to a vendor CLI rather than holding
an API key, so the secret_ref column stays empty and the user authenticates
out-of-band via the vendor's own `<tool> login` command.

## Where to look

- **Project memory (preferences, decisions):**
  `~/.claude/projects/<project-id>/memory/MEMORY.md`
- **Translator prompt content:** `backend/prompts/` — edit the `.md` files,
  not Python constants.
- **Per-backend tuning knobs:** `docs/backends.md` and `backend/config.py`.
- **Recurring gotchas:** `docs/gotchas.md`.
- **Env vars:** `.env.example`. Note that `TRANSLATOR_BACKEND` is now a
  bootstrap-seed signal only — the `providers` table is the source of truth
  after first startup.

## What NOT to do

- **Don't re-add the prohibitions** on refinement / multi-stage pipelines /
  URL scraping that older docs carried. Those were walked back on 2026-05-23.
- **Don't hardcode** `TRANSLATOR_BACKEND` lookups in new code — use the
  Provider abstraction. The legacy global stays as a fallback for the
  startup probe and tests.
- **Don't bypass the genre-aware system instruction.** Calling
  `_complete(prompt)` without first setting `self.system_instruction` (which
  `BaseTranslator.translate_chapter` does from the genre + custom_brief)
  routes a backend through an empty instruction.
- **Don't reorder or remove entries in `_ADDITIVE_MIGRATIONS`** — append-only.
