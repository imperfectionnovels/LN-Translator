# TODO

Roadmap for upcoming work on LN-Translator.

## Translation quality

- [ ] Optimize translator and refinement prompts
  - Tighten `backend/prompts/base.md` and the per-genre overlays in `backend/prompts/genres/`.
  - Audit `backend/services/translators/deepseek_revise.py` reflect/improve/revise prompts for redundancy and drift.
  - Expand `backend/prompts/examples/<genre>.md` worked examples where coverage is thin.
  - Re-baseline against a fixture chapter per genre after each prompt revision.

## Cost reduction

- [ ] Keep per-chapter API costs down
  - Trim prompt size: shrink glossary payload (top-N by recency/frequency), shorten previous-chapter tail, drop dead context blocks in `services/translators/base.py::build_prompt`.
  - Cache aggressively: extend `services/llm_cache.py` reuse and verify prompt-prefix stability so provider-side prompt caching hits.
  - Default new novels to cheaper provider tiers where quality is acceptable; surface estimated cost-per-chapter in Settings.
  - Make the refinement pass opt-in per chapter, not just per novel, so users can spot-refine instead of paying for every chapter.

## Platform coverage

- [ ] Ship macOS, Linux, and mobile builds
  - macOS: PyInstaller `--onedir` + `.app` bundle, codesign + notarize, replace WebView2 with `pywebview`'s WKWebView backend.
  - Linux: PyInstaller bundle + AppImage (or Flatpak), GTK WebKit backend for `pywebview`.
  - Mobile: evaluate a thin web client (PWA) pointed at a self-hosted backend vs. a packaged Capacitor/React Native shell; pick one path and prototype.
  - CI: extend the release workflow to build and attach macOS / Linux / mobile artifacts alongside the existing Windows EXE.
