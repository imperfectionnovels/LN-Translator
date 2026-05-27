"""Catch the regression class behind "the Translate button doesn't work".

The frozen EXE crashes mid-translate when a bundled package's sibling
data file is omitted from the PyInstaller spec. The import statement
succeeds (the .py file IS in the bundle), so static checks pass — but
the first call that touches the data file raises FileNotFoundError and
takes the worker down with it.

Two layers of defense, both pinned here:

1. ``LN-Translator.spec`` must call ``collect_data_files`` for every
   package whose runtime path reads a sibling data file. The spec
   itself is read as plain text; we don't execute it.

2. ``glossary_filters._safe_zh_convert`` is wrapped so a *future*
   regression (or running from source against a partially-installed
   environment) doesn't crash the worker. The wrapper degrades to
   unfolded text and logs once.

The tests below are deliberately strict about the contents of the spec
file so the next person to extend the bundle remembers to keep this
honest.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = REPO_ROOT / "LN-Translator.spec"

# Packages whose sibling data files (JSON / dict / template / cert
# bundle) are read at runtime and must be bundled by name. Each entry is
# the literal argument passed to ``collect_data_files`` in the spec.
#
# When adding a new third-party dep that ships data files (anything
# under its install dir that isn't a .py / .pyc / .so / .pyd / .dll),
# add the package name here too — the test fails loudly if the spec
# forgets it.
REQUIRED_DATA_PACKAGES: tuple[str, ...] = (
    "trafilatura",   # boilerplate / language models
    "certifi",       # TLS CA bundle
    "ebooklib",      # EPUB ZIP handling shims
    "docx",          # python-docx default-template XML + oxml fragments
    "zhconv",        # simplified ↔ traditional dict (zhcdict.json)
)


def _spec_text() -> str:
    assert SPEC_PATH.exists(), f"LN-Translator.spec not found at {SPEC_PATH}"
    return SPEC_PATH.read_text(encoding="utf-8")


@pytest.mark.parametrize("pkg", REQUIRED_DATA_PACKAGES)
def test_spec_collects_data_files_for(pkg: str) -> None:
    text = _spec_text()
    needle = f'collect_data_files("{pkg}")'
    alt = f"collect_data_files('{pkg}')"
    assert needle in text or alt in text, (
        f"LN-Translator.spec is missing `collect_data_files({pkg!r})`. "
        f"Without it, {pkg}'s sibling data files won't ship in the frozen "
        f"bundle and the EXE will crash the first time the runtime path "
        f"touches them. Add the call to the `datas += ...` block."
    )


def test_safe_zh_convert_degrades_when_dict_missing(monkeypatch) -> None:
    """The wrapper survives a FileNotFoundError from zhconv.convert.

    Simulates the misbuilt-bundle case: the import succeeds, the dict
    load raises. The worker must not crash; the function returns the
    input unchanged and logs once.
    """
    import backend.services.glossary_filters as gf

    # Reset the disabled flag so this test is independent of any other
    # test that may have tripped it.
    gf._ZHCONV_DISABLED = False

    def _raise(*_a, **_kw):
        raise FileNotFoundError("zhcdict.json")

    monkeypatch.setattr(gf, "_zh_convert", _raise)

    out = gf._safe_zh_convert("測試", "zh-hans")
    assert out == "測試", "wrapper should return input unchanged on data-file failure"
    # Once disabled, subsequent calls short-circuit without re-raising
    # — meaning a misbuilt bundle hits the log line at most once per
    # process, not 1900 times during a glossary merge.
    out2 = gf._safe_zh_convert("劍仙", "zh-hans")
    assert out2 == "劍仙"
    assert gf._ZHCONV_DISABLED is True

    # Reset so the rest of the suite gets the real behavior.
    gf._ZHCONV_DISABLED = False


def test_canonical_zh_uses_safe_wrapper() -> None:
    """canonical_zh routes through _safe_zh_convert, not _zh_convert directly.

    Guards against a future refactor that re-introduces the un-wrapped
    call site — the failure mode was hard to spot precisely because the
    crash happened inside a one-line helper called from the worker.
    """
    import re

    src = (REPO_ROOT / "backend" / "services" / "glossary_filters.py").read_text(encoding="utf-8")
    canonical_block = src.split("def canonical_zh", 1)[1].split("\ndef ", 1)[0]
    assert "_safe_zh_convert" in canonical_block, (
        "canonical_zh must call _safe_zh_convert so a missing zhcdict.json "
        "can't crash the worker."
    )
    # The function body must NOT call the un-wrapped _zh_convert. Use a
    # word-boundary regex so the substring inside _safe_zh_convert isn't
    # mistaken for a bare call.
    bare_call = re.compile(r"(?<![A-Za-z0-9_])_zh_convert\s*\(")
    assert not bare_call.search(canonical_block), (
        "canonical_zh must NOT call _zh_convert directly — route through "
        "_safe_zh_convert so a bundle regression degrades gracefully."
    )


def test_lifespan_probes_runtime_data() -> None:
    """The boot path must call _probe_bundled_runtime_data.

    Pinning this so a future refactor of main.lifespan doesn't quietly
    drop the canary that surfaces misbuilt-bundle conditions in
    startup.log.
    """
    src = (REPO_ROOT / "backend" / "main.py").read_text(encoding="utf-8")
    assert "_probe_bundled_runtime_data" in src, (
        "backend/main.py must define _probe_bundled_runtime_data — it's "
        "the boot-time canary that surfaces misbuilt-bundle conditions."
    )
    # And the lifespan must actually invoke it.
    lifespan_block = src.split("async def lifespan", 1)[1].split("\napp =", 1)[0]
    assert "_probe_bundled_runtime_data" in lifespan_block, (
        "lifespan() must invoke _probe_bundled_runtime_data() at boot."
    )
