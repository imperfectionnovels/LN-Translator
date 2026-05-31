# External tooling assessment (MCP servers, skills, plugins)

A deeper survey than the first pass, covering whether any Claude Code MCP server,
skill, or plugin can help LN-Translator across efficiency, operations, and
translation. Dated 2026-05-30. This is a durable reference so the survey does not
have to be re-run from scratch.

## The reframing that matters

There are two separate layers, and conflating them is what makes "which MCP improves
my translation?" the wrong question:

1. **The app's runtime translation** runs server-side in the FastAPI backend, calling
   top-tier providers (Claude Opus 4.8, Gemini, DeepSeek, ...) through
   `backend/services/translators/`. An MCP server wired into *Claude Code* (the dev
   environment) never executes on that path. So **no MCP server can be "plugged in"
   to make the app translate better.** Anything that improves the product's output
   is either a prompt change, a provider change, or new backend code, not a connector.

2. **Tooling that helps the developer (me)** build, navigate, test, scrape-for, and
   ship the app. This is where external tooling genuinely pays off.

A corollary: the localization-industry translation MCPs (DeepL, LILT, Smartling,
Lara, Azure) are built for UI strings, manuals, and TMX memories. For literary
xianxia/wuxia prose with a custom voice, their NMT output is a downgrade from what the
app already runs. They are not relevant as a translation engine here.

## Verdicts at a glance

| Tool | Category | Layer | Verdict |
| --- | --- | --- | --- |
| Playwright MCP | Web automation | Dev | **Adopt** (scraper-recipe development) |
| `glossary-audit` skill (new) | Translation QA | Dev | **Adopt** (build it; companion to `fix-glossary`) |
| Serena MCP | Code intelligence | Dev | **Optional** (needs a one-time `uv` install) |
| SQLite read-only MCP (`sqlite-dev`/`sqlite-live`) | DB inspection | Dev | **Already in use, keep** |
| IDE diagnostics MCP (`getDiagnostics`) | Lint/type signal | Dev | **Already connected, use it** |
| GitHub Actions release-on-tag | Ops/release | App repo | **Build-it-yourself** (offered) |
| A/B-test harness skill | Translation QA | Dev | **Build-it-yourself** (offered) |
| DeepL / LILT / Smartling / Lara / Azure | Translation engine | Runtime | **Skip** (localization, not literary) |
| Firecrawl MCP | Web scraping | Dev | **Skip** (Playwright covers it, no API key) |
| Official GitHub MCP server | Release ops | Dev | **Skip** (`gh` CLI already does it; local server wants Docker) |
| Context7 MCP | Library docs | Dev | **Skip** (libs stable, within model knowledge) |
| Pyright / Pylance MCP | Type checking | Dev | **Skip** (loosely-typed codebase, noisy; IDE MCP covers signal) |
| pytest-runner MCP | Test running | Dev | **Skip** (pytest via Bash already works) |
| pandoc / markitdown MCP | Doc conversion | Runtime | **Skip** (app already parses/exports; bundle bloat risk) |
| CC-CEDICT / COMET-xCOMET | Glossary/QA data | Runtime | **Skip** (poor fit for cultivation terms / literary quality) |

## Adopt

### Playwright MCP (`microsoft/playwright-mcp`)

Official Microsoft browser-automation MCP. Drives a real Chromium via accessibility
snapshots (no vision model needed). Node 24 + npx are already installed, so the only
one-time cost is a Chromium download (~150 MB).

**Why it fits:** the scraper is the app's maintenance hotspot.
`backend/services/scraper.py` plus the per-site recipes under
`backend/services/scrapers/` (and the Cloudflare chain in `scrapers/cloudflare.py`)
break whenever a novel site changes its markup or anti-bot posture. With Playwright I
can load a live chapter page, inspect the rendered DOM, find the chapter-list / title
/ body selectors, and reproduce a Cloudflare challenge, then translate that into a new
or fixed recipe. It does not run inside the app and does not replace the runtime
scraper; it is a recipe-development tool for the dev loop.

Config (in `.mcp.json`):
```json
"playwright": { "command": "npx", "args": ["-y", "@playwright/mcp@latest"] }
```
Windows fallback if `npx` will not spawn directly: `"command": "cmd", "args": ["/c",
"npx", "-y", "@playwright/mcp@latest"]`.

### `glossary-audit` skill (build it)

A proactive companion to the existing reactive `fix-glossary` skill. It runs read-only
SQL through the `sqlite-dev` / `sqlite-live` MCP and surfaces the defect classes that
are grounded in `backend/services/glossary_casing.py`:

- **Synonym collisions:** different `term_zh` mapping to the same `term_en`
  (case-insensitive). After a casing fix on one, `is_atomic_case_locked_term`
  re-imposes the casing via the other (the documented `神识`/`神念` to "Divine Sense"
  trap).
- **Casing inconsistency:** the same English rendered with different casing across
  rows.
- **Unreviewed auto-detected noise:** `auto_detected=1 AND locked=0` rows, optionally
  cross-referenced against the novel's `translated_text` to flag renderings that never
  actually appear (a candidate the model never used).
- **Missing-alias hints:** variant Chinese forms that should mirror a canonical row.

Audit only, no DB writes; it hands off to `fix-glossary` to apply. Pure markdown, no
install. Stored at `.claude/skills/glossary-audit/SKILL.md` (gitignored, local-only,
like `fix-glossary`).

## Use what you already have

- **SQLite read-only MCP** (`sqlite-dev` + `sqlite-live`, `tools/sqlite_ro_mcp.py`):
  the right tool for glossary/chapter inspection without stopping the running EXE.
  Keep using it instead of throwaway scripts; the new `glossary-audit` skill builds on
  it.
- **IDE diagnostics MCP** (`getDiagnostics`): already connected. Useful for catching
  syntax/type/lint signal without a separate Pyright wiring.
- **LILT MCP** is connected to your Claude account but is enterprise localization;
  ignore it for this project.
- **Gamma / Gmail / Google Calendar / Drive MCPs** are connected but tangential to the
  codebase. Drive could be a manual backup target for the DB or release zips if ever
  wanted; otherwise ignore.

## Build-it-yourself (offered, not auto-wired)

These are the real translation-quality and ops wins, but they are feature work, not
tools to adopt:

- **GitHub Actions release-on-tag** (`.github/workflows/release.yml`): on a `v*` tag,
  build the EXE on `windows-latest`, run the smoke test (`scripts/smoke-exe.ps1`),
  and upload the zip to the GitHub release. Removes the manual rebuild-and-reupload
  toil. Currently only `ci.yml` (test) exists.
- **A/B-test harness skill:** run one chapter under two prompt-flag configs
  (e.g. `PROMPT_INCLUDE_FREE_DRAFT` on/off), diff the outputs, and check the binding
  graduation rule in `CLAUDE.md`. Leans on the `chapters.prompt_config_snapshot`
  provenance already written by `services/queue.py::_build_prompt_config_snapshot`.

## Skip (with reasons)

- **DeepL / LILT / Smartling / Lara / Azure as a translation engine:** enterprise
  localization NMT, not literary; a downgrade from Opus 4.8 for cultivation prose.
- **Firecrawl MCP:** Playwright covers the inspection need with no API key and no paid
  tier.
- **Official GitHub MCP server:** the `gh` CLI already handles release uploads
  (`gh release upload --clobber`); the local server wants Docker (absent), and the
  remote one adds an OAuth dependency for no gain over `gh`.
- **Context7 MCP:** the stack (FastAPI, aiosqlite, pydantic, httpx) is stable and
  within the model's knowledge; marginal value, adds an API key + Node dependency.
- **Pyright / Pylance MCP:** the codebase leans on `aiosqlite.Row`/`dict`, so a strict
  type checker emits mostly noise; the IDE diagnostics MCP already provides the signal
  worth having.
- **pytest-runner MCP:** running `pytest backend/tests` via Bash already works and the
  output is readable; the structured-triage upside is small for one developer.
- **pandoc / markitdown MCP:** the app already decodes txt/docx/epub/html
  (`services/uploads.py`) and exports epub (`services/epub_export.py`); adding these as
  backend deps risks PyInstaller bundle bloat for no parsing gain.
- **CC-CEDICT / COMET / xCOMET:** cultivation terms render poorly in a general CN-EN
  dictionary, and BLEU/COMET-style metrics do not measure literary quality; neither
  earns a place in the pipeline.

## Install constraints on this machine (for future reference)

- Node v24 + npx: present (Playwright, any npx-based MCP works).
- `uv` / `uvx`: absent (Serena and uvx-launched servers need a one-time `uv` install).
- Docker: absent (rules out the local GitHub MCP server image).
- `gh` CLI: present (the release path).
- Default `python` is 3.14; the SQLite MCP runs on the pinned `pythoncore-3.14`
  interpreter. The app itself targets 3.11+.
- `.mcp.json`, `.claude/skills/`, and `.serena/` are gitignored: MCP/skill wiring is
  per-machine and local-only, not committed.
