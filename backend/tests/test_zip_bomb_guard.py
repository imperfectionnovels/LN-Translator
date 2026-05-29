"""C2: decompression-bomb guard for ZIP-container uploads (EPUB / DOCX).

MAX_UPLOAD_BYTES caps only the COMPRESSED upload, so a small archive can
declare gigabytes of uncompressed content and OOM the process when ebooklib /
python-docx inflate it. `_reject_zip_bomb` reads the central directory's
declared sizes (no inflation) and rejects an implausible ratio or total.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi import HTTPException

from backend.services.uploads import _reject_zip_bomb


def _zip_with_entry(name: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, data)
    return buf.getvalue()


def test_reject_zip_bomb_flags_implausible_ratio():
    # 4 MB of zeros deflates to a few KB -> ratio >> 100 and size > 1 MB.
    raw = _zip_with_entry("big.bin", b"\x00" * (4 * 1024 * 1024))
    with pytest.raises(HTTPException) as ei:
        _reject_zip_bomb(raw, "bomb.epub")
    assert ei.value.status_code == 400
    assert "decompression bomb" in ei.value.detail


def test_reject_zip_bomb_passes_normal_archive():
    raw = _zip_with_entry("ch1.xhtml", b"<p>" + b"normal chapter text. " * 200 + b"</p>")
    _reject_zip_bomb(raw, "ok.epub")  # must not raise


def test_reject_zip_bomb_ignores_non_zip():
    # Non-zip bytes fall through; the format parser surfaces its own error.
    _reject_zip_bomb(b"not a zip at all", "x.docx")  # must not raise
