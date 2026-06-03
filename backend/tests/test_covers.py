"""Unit tests for services/covers.py pure helpers.

Covers the two host-independent functions that had no coverage:

  * `sniff_image_ext` — magic-byte image-type detection (PNG/JPEG/GIF/WebP).
  * `resolve_cover_path` — maps a stored relative path to an absolute one
    under USER_DATA_ROOT, with a path-traversal guard that rejects any
    stored value resolving outside the data root (defense against a
    tampered novels.cover_image_path row).

The heavier write_cover_for_novel (DB + atomic file write) is exercised
through the cover-upload route tests; here we pin the cheap, security-
relevant primitives directly.
"""

from __future__ import annotations

import pytest

from backend.services import covers


@pytest.mark.parametrize(
    "head, expected",
    [
        (b"\x89PNG\r\n\x1a\n....", "png"),
        (b"\xff\xd8\xff\xe0....", "jpg"),
        (b"GIF87a....", "gif"),
        (b"GIF89a....", "gif"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "webp"),
        (b"not an image", None),
        (b"", None),
        (b"RIFF\x00\x00\x00\x00WAVEfmt ", None),  # RIFF container, not WEBP
    ],
)
def test_sniff_image_ext(head, expected):
    assert covers.sniff_image_ext(head) == expected


def test_resolve_cover_path_none_and_empty():
    assert covers.resolve_cover_path(None) is None
    assert covers.resolve_cover_path("") is None


def test_resolve_cover_path_returns_absolute_for_real_file(tmp_path, monkeypatch):
    monkeypatch.setattr(covers, "USER_DATA_ROOT", tmp_path)
    covers_dir = tmp_path / "covers"
    covers_dir.mkdir()
    (covers_dir / "1.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    resolved = covers.resolve_cover_path("covers/1.png")
    assert resolved is not None
    assert resolved == (tmp_path / "covers" / "1.png").resolve()


def test_resolve_cover_path_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(covers, "USER_DATA_ROOT", tmp_path)
    assert covers.resolve_cover_path("covers/does-not-exist.png") is None


def test_resolve_cover_path_rejects_traversal_outside_root(tmp_path, monkeypatch):
    monkeypatch.setattr(covers, "USER_DATA_ROOT", tmp_path)
    # Even if a real file exists just outside the root, a stored value that
    # escapes USER_DATA_ROOT must resolve to None.
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("secret")
    assert covers.resolve_cover_path("../secret.txt") is None
