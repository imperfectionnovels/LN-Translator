"""OPUS-MT model lifecycle: registry, download, load, segment, sentinel.

This module owns everything about the free-tier OPUS-MT NMT backend that is
**not** the translator class itself. It is intentionally lazy-import-only —
nothing here should trigger ctranslate2 or sentencepiece at module load, so
the rest of the app boots without the OPUS-MT wheels installed.

Layout under ``USER_DATA_ROOT/opus_mt/<pair>/`` after install:
    model.bin               CTranslate2 model weights
    config.json             CT2 config
    shared_vocabulary.json  (if applicable; some pairs use separate vocabs)
    source.spm              SentencePiece source tokenizer
    target.spm              SentencePiece target tokenizer

A "pair" is a two-letter source language code plus ``-en``: ``zh-en``,
``ja-en``, ``ko-en``. The pair is also the provider's ``model_id`` in the
catalog, so ``OpusMTTranslator`` can map source-language → installed model
by reading ``provider.model_id``.

Pre-converted CT2 bundles are published as GitHub release assets on this
repo (see ``_DEFAULT_RELEASE_TAG`` below). URLs + SHA256 digests can be
overridden via env vars for dev / staging — useful when iterating on a new
pair before promoting it to the production release tag.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import string
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable, Optional

import httpx

from backend.config import USER_DATA_ROOT

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_MODELS_SUBDIR = "opus_mt"
_DEFAULT_RELEASE_TAG = "opus-mt-v1"
_REPO_RELEASE_BASE = (
    "https://github.com/ImperfectionNovels/LN-Translator/releases/download"
)


@dataclass(frozen=True)
class ModelSpec:
    """Static metadata for one language-pair bundle."""

    pair: str
    source_lang: str            # ISO 639-1 — matches novels.source_language
    target_lang: str
    url: str                    # https URL to the .tar.gz bundle
    sha256: str                 # hex digest; "" disables verification (dev only)
    size_mb: int                # rough installed footprint, for UI display
    display: str                # human label for the UI


def _default_url(pair: str) -> str:
    tag = os.environ.get("OPUS_MT_RELEASE_TAG", _DEFAULT_RELEASE_TAG)
    return f"{_REPO_RELEASE_BASE}/{tag}/opus-mt-{pair}.tar.gz"


def _env_or(key_url: str, key_sha: str, pair: str) -> tuple[str, str]:
    url = os.environ.get(key_url, "").strip() or _default_url(pair)
    sha = os.environ.get(key_sha, "").strip()
    return url, sha


# SHA256 digests of the tarballs published to the opus-mt-v1 GitHub release.
# Bumping a bundle = bump the digest here AND re-upload the asset.
_BUNDLED_SHA256 = {
    "zh-en": "83ee3dd23b26d42150f4eb9656a4a4d611c56e9dd269f1b14de44a2b8782d6c7",
    "ja-en": "e0efe11c3e40576ecf93eebd7cc2917c05c2ec5bb0fd09a027622ac692a4776c",
    "ko-en": "d85ed718c3e07ac2208b8e6cedeac0fd9a5644245f6a3334eca196edc20f2d6b",
}


def _build_registry() -> dict[str, ModelSpec]:
    zh_url, zh_sha = _env_or("OPUS_MT_ZH_EN_URL", "OPUS_MT_ZH_EN_SHA256", "zh-en")
    ja_url, ja_sha = _env_or("OPUS_MT_JA_EN_URL", "OPUS_MT_JA_EN_SHA256", "ja-en")
    ko_url, ko_sha = _env_or("OPUS_MT_KO_EN_URL", "OPUS_MT_KO_EN_SHA256", "ko-en")
    # Env override wins, otherwise fall back to the bundled digest. This lets
    # dev/staging point at an unverified URL via OPUS_MT_*_URL alone without
    # also having to clear an inherited digest.
    zh_sha = zh_sha or _BUNDLED_SHA256["zh-en"]
    ja_sha = ja_sha or _BUNDLED_SHA256["ja-en"]
    ko_sha = ko_sha or _BUNDLED_SHA256["ko-en"]
    return {
        "zh-en": ModelSpec(
            pair="zh-en", source_lang="zh", target_lang="en",
            url=zh_url, sha256=zh_sha, size_mb=78,
            display="Chinese → English",
        ),
        "ja-en": ModelSpec(
            pair="ja-en", source_lang="ja", target_lang="en",
            url=ja_url, sha256=ja_sha, size_mb=76,
            display="Japanese → English",
        ),
        "ko-en": ModelSpec(
            pair="ko-en", source_lang="ko", target_lang="en",
            url=ko_url, sha256=ko_sha, size_mb=78,
            display="Korean → English",
        ),
    }


SUPPORTED_PAIRS: dict[str, ModelSpec] = _build_registry()


def pair_for_language(source_language: str) -> str | None:
    """Map a novel's source_language to the canonical OPUS-MT pair key."""
    for pair, spec in SUPPORTED_PAIRS.items():
        if spec.source_lang == source_language:
            return pair
    return None


def model_dir(pair: str) -> Path:
    """Per-pair installed-model directory under USER_DATA_ROOT."""
    if pair not in SUPPORTED_PAIRS:
        raise KeyError(f"unknown OPUS-MT pair {pair!r}")
    return USER_DATA_ROOT / _MODELS_SUBDIR / pair


_REQUIRED_FILES = ("model.bin", "source.spm", "target.spm")


def is_installed(pair: str) -> bool:
    """A pair counts as installed iff its directory has the required artifacts."""
    try:
        root = model_dir(pair)
    except KeyError:
        return False
    return all((root / f).is_file() for f in _REQUIRED_FILES)


def installed_size_mb(pair: str) -> int:
    """Sum file sizes under the pair's dir, in MB. Returns 0 if not installed."""
    root = model_dir(pair) if pair in SUPPORTED_PAIRS else None
    if root is None or not root.is_dir():
        return 0
    total = 0
    for entry in root.rglob("*"):
        if entry.is_file():
            total += entry.stat().st_size
    return total // (1024 * 1024)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProgressEvent:
    """One tick from the streaming download. ``total`` may be 0 if the server
    didn't send Content-Length; treat as indeterminate in that case."""

    pair: str
    phase: str               # 'downloading' | 'verifying' | 'extracting' | 'done' | 'error'
    bytes_done: int
    bytes_total: int
    detail: str = ""         # error message in 'error' phase; otherwise empty


# Per-pair locks so concurrent download requests for the same pair collapse
# without re-downloading. A second caller awaiting the lock observes
# ``is_installed`` True on entry and returns immediately.
_download_locks: dict[str, asyncio.Lock] = {}


def _lock_for(pair: str) -> asyncio.Lock:
    lock = _download_locks.get(pair)
    if lock is None:
        lock = asyncio.Lock()
        _download_locks[pair] = lock
    return lock


async def download_pair(
    pair: str,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> AsyncIterator[ProgressEvent]:
    """Stream-download a pair bundle, verify (if SHA256 set), and extract atomically.

    Atomic via a sibling temp directory: bytes land in
    ``<USER_DATA_ROOT>/opus_mt/.<pair>-staging-<rand>/`` and only after
    successful extraction + verification does ``os.replace`` move it into
    ``<USER_DATA_ROOT>/opus_mt/<pair>/``. A crash mid-download leaves the
    staging dir behind (cleaned on next download attempt by the temp-prefix
    sweeper) but never produces a half-installed pair dir.

    Yields ProgressEvent during the download and exits after the 'done' event
    (or one 'error' event followed by an exception).
    """
    if pair not in SUPPORTED_PAIRS:
        raise KeyError(f"unknown OPUS-MT pair {pair!r}")
    spec = SUPPORTED_PAIRS[pair]
    if not spec.url:
        raise RuntimeError(
            f"OPUS-MT pair {pair!r} has no download URL configured. Set "
            f"OPUS_MT_{pair.upper().replace('-', '_')}_URL or wait for the "
            f"production release tag to ship the bundle."
        )

    lock = _lock_for(pair)
    async with lock:
        if is_installed(pair):
            yield ProgressEvent(pair=pair, phase="done", bytes_done=0, bytes_total=0)
            return

        dest_dir = model_dir(pair)
        dest_dir.parent.mkdir(parents=True, exist_ok=True)

        # Sweep stale staging dirs from prior crashes.
        for stale in dest_dir.parent.glob(f".{pair}-staging-*"):
            shutil.rmtree(stale, ignore_errors=True)

        staging = Path(tempfile.mkdtemp(prefix=f".{pair}-staging-", dir=dest_dir.parent))
        tar_path = staging / "bundle.tar.gz"

        client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0),
            follow_redirects=True,
        )
        own_client = http_client is None

        sha = hashlib.sha256()
        bytes_done = 0
        try:
            async with client.stream("GET", spec.url) as resp:
                resp.raise_for_status()
                bytes_total = int(resp.headers.get("Content-Length", 0))
                with tar_path.open("wb") as fp:
                    async for chunk in resp.aiter_bytes(chunk_size=1 << 16):
                        fp.write(chunk)
                        sha.update(chunk)
                        bytes_done += len(chunk)
                        yield ProgressEvent(
                            pair=pair,
                            phase="downloading",
                            bytes_done=bytes_done,
                            bytes_total=bytes_total,
                        )

            if spec.sha256:
                yield ProgressEvent(
                    pair=pair, phase="verifying",
                    bytes_done=bytes_done, bytes_total=bytes_done,
                )
                digest = sha.hexdigest()
                if digest.lower() != spec.sha256.lower():
                    raise RuntimeError(
                        f"SHA256 mismatch for {pair}: got {digest}, expected {spec.sha256}"
                    )

            yield ProgressEvent(
                pair=pair, phase="extracting",
                bytes_done=bytes_done, bytes_total=bytes_done,
            )
            extract_to = staging / "extracted"
            extract_to.mkdir()
            with tarfile.open(tar_path, "r:gz") as tf:
                _safe_extract(tf, extract_to)

            # The tar may have one top-level dir (e.g., opus-mt-zh-en/) or
            # flat files. Resolve to the single dir if there is one.
            extracted_root = _resolve_extracted_root(extract_to)
            missing = [f for f in _REQUIRED_FILES if not (extracted_root / f).is_file()]
            if missing:
                raise RuntimeError(
                    f"OPUS-MT bundle for {pair} is missing required files: {missing}"
                )

            if dest_dir.exists():
                shutil.rmtree(dest_dir)
            os.replace(extracted_root, dest_dir)
            yield ProgressEvent(
                pair=pair, phase="done",
                bytes_done=bytes_done, bytes_total=bytes_done or bytes_done,
            )
        except Exception as exc:
            yield ProgressEvent(
                pair=pair, phase="error",
                bytes_done=bytes_done, bytes_total=0,
                detail=str(exc),
            )
            raise
        finally:
            if own_client:
                await client.aclose()
            shutil.rmtree(staging, ignore_errors=True)


def _safe_extract(tf: tarfile.TarFile, dest: Path) -> None:
    """Extract a tar safely — refuses members that would escape ``dest`` via
    absolute paths or ``..`` traversal. Matches the recipe Python 3.12 ships
    as ``tarfile.data_filter`` but avoids the version dependency."""
    dest_resolved = dest.resolve()
    for member in tf.getmembers():
        target = (dest / member.name).resolve()
        try:
            target.relative_to(dest_resolved)
        except ValueError as exc:
            raise RuntimeError(
                f"refusing to extract {member.name!r}: escapes destination"
            ) from exc
    tf.extractall(dest)


def _resolve_extracted_root(extract_to: Path) -> Path:
    """If the tar contained a single top-level dir, return it; else return the
    extract dir itself."""
    children = [p for p in extract_to.iterdir() if not p.name.startswith(".")]
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return extract_to


def remove_pair(pair: str) -> bool:
    """Delete the on-disk model for a pair. Returns True if it existed."""
    if pair not in SUPPORTED_PAIRS:
        raise KeyError(f"unknown OPUS-MT pair {pair!r}")
    root = model_dir(pair)
    if not root.exists():
        return False
    shutil.rmtree(root)
    # Drop any cached translator instance for this pair so a later install
    # is picked up cleanly.
    _translator_cache.pop(pair, None)
    return True


# ---------------------------------------------------------------------------
# Sentence segmentation (paragraph-preserving CJK splitter)
# ---------------------------------------------------------------------------

# Sentence-terminal punctuation we split on. Includes the CJK fullwidth
# variants and a few ASCII fallbacks for romanized inserts.
_SENT_END = set("。！？!?")

# Quote-pair openers/closers. We do not split inside a balanced quote span,
# because dialogue beats often end on a sentence-terminal punctuation that
# the speaker said, not that the narrator's sentence ended.
_QUOTE_OPEN = set("「『“‘《")
_QUOTE_CLOSE = set("」』”’》")


def split_sentences(paragraph: str) -> list[str]:
    """Split one paragraph into sentences, respecting quote pairs.

    Pure regex-free state-machine: we walk the string once, buffering until
    we hit a sentence-terminal punctuation while not inside a quote. The
    punctuation stays attached to the sentence it ended.
    """
    if not paragraph:
        return []
    out: list[str] = []
    buf: list[str] = []
    quote_depth = 0
    for ch in paragraph:
        buf.append(ch)
        if ch in _QUOTE_OPEN:
            quote_depth += 1
        elif ch in _QUOTE_CLOSE and quote_depth > 0:
            quote_depth -= 1
        elif ch in _SENT_END and quote_depth == 0:
            piece = "".join(buf).strip()
            if piece:
                out.append(piece)
            buf = []
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n+")


def segment_paragraphs(text: str) -> list[list[str]]:
    """Split a chapter into a list of paragraphs, each a list of sentences.

    Paragraph boundaries are blank-line separated. Empty paragraphs collapse
    to empty inner lists, which the reassembly step uses to preserve the
    original blank-line rhythm.
    """
    if not text:
        return []
    paragraphs = _PARAGRAPH_SPLIT.split(text.strip("\n"))
    return [split_sentences(p) for p in paragraphs]


def reassemble(paragraphs: list[list[str]]) -> str:
    """Inverse of segment_paragraphs: re-stitch with a single space between
    sentences inside a paragraph and one blank line between paragraphs."""
    pieces = [" ".join(s for s in para if s) for para in paragraphs]
    return "\n\n".join(p for p in pieces if p)


# ---------------------------------------------------------------------------
# Glossary placeholder sentinels
# ---------------------------------------------------------------------------

# Candidate sentinel formats, in order of preference (shortest first). Each
# format is a callable ``int -> str`` so the substitution layer can mint
# distinct sentinels per glossary entry. We probe them at load time against
# the installed pair's source tokenizer and pick the first format whose
# encode→decode roundtrip is lossless.
_SENTINEL_CANDIDATES: tuple[Callable[[int], str], ...] = (
    lambda i: f"ZX{i:03d}",        # ZX001, ZX002, ...
    lambda i: f"XZX{i:02d}",       # XZX01, XZX02, ...
    lambda i: f"ZZZZX{i:03d}",     # ZZZZX001, ...
    lambda i: f"X{i:03d}ZZZZ",
)


def _probe_sentinel(
    fmt: Callable[[int], str], src_tokenizer, tgt_tokenizer
) -> bool:
    """One sentinel format survives if encode→decode of a small batch of
    instances yields the exact strings back, with no internal whitespace
    insertion or piece-split. We probe several indices, not just one, so a
    digit-specific corner case doesn't slip through."""
    samples = [fmt(i) for i in (1, 7, 42, 123)]
    inline = "Foo " + " ".join(samples) + " bar"
    encoded = src_tokenizer.encode(inline, out_type=str)
    decoded = src_tokenizer.decode(encoded)
    if any(s not in decoded for s in samples):
        return False
    # Also confirm the target tokenizer (used on the way out for post-
    # substitution scanning) can re-detect the sentinel. We only care that
    # the literal strings appear in the target's decoded output.
    out_encoded = tgt_tokenizer.encode(inline, out_type=str)
    out_decoded = tgt_tokenizer.decode(out_encoded)
    return all(s in out_decoded for s in samples)


def choose_sentinel_format(
    src_tokenizer, tgt_tokenizer
) -> Callable[[int], str] | None:
    """Return the first sentinel format whose roundtrip is lossless on both
    tokenizers, or None if no format survives (caller falls back to no
    substitution and accepts terminology drift)."""
    for fmt in _SENTINEL_CANDIDATES:
        try:
            if _probe_sentinel(fmt, src_tokenizer, tgt_tokenizer):
                return fmt
        except Exception as exc:
            logger.debug(
                "OPUS-MT sentinel candidate failed during probe: %s", exc
            )
    logger.warning(
        "OPUS-MT: no glossary sentinel format survived SentencePiece roundtrip "
        "on the installed pair — locked-term substitution disabled, expect "
        "terminology drift."
    )
    return None


# ---------------------------------------------------------------------------
# Translator loader
# ---------------------------------------------------------------------------

class CTranslator:
    """Thin wrapper bundling a CTranslate2 Translator plus the two
    SentencePiece tokenizers and the chosen sentinel format.

    Per-instance state is loaded lazily by ``load_translator`` and cached in
    ``_translator_cache`` so a long-running worker amortizes the model load
    across many chapters."""

    def __init__(
        self,
        pair: str,
        ct2_translator,
        src_tokenizer,
        tgt_tokenizer,
        sentinel_fn: Callable[[int], str] | None,
    ) -> None:
        self.pair = pair
        self._ct2 = ct2_translator
        self._src = src_tokenizer
        self._tgt = tgt_tokenizer
        self.sentinel_fn = sentinel_fn

    def translate_batch(self, sentences: list[str]) -> list[str]:
        """Translate a list of source-language sentences. The list may be
        empty; the result is the per-element translation in the same order.
        SentencePiece handles whitespace; we do not mutate inputs here."""
        if not sentences:
            return []
        tokens_in = [self._src.encode(s, out_type=str) for s in sentences]
        result = self._ct2.translate_batch(
            tokens_in,
            beam_size=4,
            max_decoding_length=256,
            replace_unknowns=True,
        )
        tokens_out = [r.hypotheses[0] for r in result]
        return [self._tgt.decode(t) for t in tokens_out]


_translator_cache: dict[str, CTranslator] = {}


def load_translator(pair: str) -> CTranslator:
    """Lazy singleton: load the CT2 Translator + tokenizers for ``pair`` once
    per process, return the cached instance on subsequent calls.

    Raises ``FileNotFoundError`` if the model isn't installed (caller maps
    this to the friendlier "open Settings to download" error). Raises
    ``ImportError`` if ctranslate2 / sentencepiece aren't available, which
    is the boot-time graceful-degrade signal."""
    cached = _translator_cache.get(pair)
    if cached is not None:
        return cached

    if not is_installed(pair):
        raise FileNotFoundError(
            f"OPUS-MT model for pair {pair!r} is not installed. Download it "
            "from Settings → Providers."
        )

    try:
        import ctranslate2
        import sentencepiece as spm
    except ImportError as exc:  # pragma: no cover — verified at install time
        raise ImportError(
            "ctranslate2 and sentencepiece are required for the free-tier "
            "OPUS-MT backend. Reinstall with `pip install -e .` to pick them up."
        ) from exc

    root = model_dir(pair)
    ct2_translator = ctranslate2.Translator(
        str(root),
        device="cpu",
        compute_type="int8",
        intra_threads=max(1, (os.cpu_count() or 2)),
        inter_threads=1,
    )
    src_tok = spm.SentencePieceProcessor(model_file=str(root / "source.spm"))
    tgt_tok = spm.SentencePieceProcessor(model_file=str(root / "target.spm"))

    sentinel_fn = choose_sentinel_format(src_tok, tgt_tok)
    inst = CTranslator(pair, ct2_translator, src_tok, tgt_tok, sentinel_fn)
    _translator_cache[pair] = inst
    return inst


def evict_translator(pair: str) -> None:
    """Drop a cached translator instance. Tests + remove_pair use this."""
    _translator_cache.pop(pair, None)


# Allowed punctuation characters inside sentinel format strings — kept so a
# future format author can't slip in something that conflicts with our
# delimited-envelope parser.
_SENTINEL_ALLOWED = set(string.ascii_letters + string.digits + "-_")


def validate_sentinel(s: str) -> bool:
    """Cheap structural check: a candidate sentinel must be ASCII and use
    only ``[A-Za-z0-9_-]``. Stops invalid formats from being added to
    ``_SENTINEL_CANDIDATES`` without a probe."""
    return bool(s) and all(c in _SENTINEL_ALLOWED for c in s)
