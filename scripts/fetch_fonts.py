"""
fetch_fonts.py -- dev-only font acquisition script.

Downloads and subsets Fraunces (variable), Spectral (static), and
Noto Serif SC (Google Fonts unicode-range slices) into frontend/fonts/.
Generates frontend/css/fonts.css with self-hosted @font-face blocks.

Regenerate: python scripts/fetch_fonts.py
Requires: fonttools, brotli  (pip install fonttools brotli)
No runtime deps added; this script is never packaged.
"""

import os
import re
import sys
import json
import urllib.request
import urllib.parse
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO = Path(__file__).resolve().parent.parent
FONTS_DIR = REPO / "frontend" / "fonts"
CSS_OUT   = REPO / "frontend" / "css" / "fonts.css"

FRAUNCES_DIR = FONTS_DIR / "fraunces"
SPECTRAL_DIR = FONTS_DIR / "spectral"
NOTO_DIR     = FONTS_DIR / "noto-serif-sc"

for d in (FRAUNCES_DIR, SPECTRAL_DIR, NOTO_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Latin subset unicode ranges
# ---------------------------------------------------------------------------
LATIN_UNICODES = (
    "U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,"
    "U+2000-206F,U+2074,U+20AC,U+2122,U+2190-2199,U+2212,U+2215,U+FEFF,U+FFFD"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fetch(url: str, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def save(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    print(f"  saved {path.relative_to(REPO)}  ({len(data):,} bytes)")


def subset_to_woff2(src: Path, dst: Path, unicodes: str, extra_args: list[str] | None = None) -> None:
    """Subset src TTF to dst woff2 using fontTools.subset programmatically."""
    from fontTools.subset import main as ft_main  # type: ignore
    args = [
        str(src),
        f"--output-file={dst}",
        f"--unicodes={unicodes}",
        "--layout-features=*",
        "--flavor=woff2",
    ]
    if extra_args:
        args.extend(extra_args)
    ft_main(args)
    print(f"  subset -> {dst.relative_to(REPO)}  ({dst.stat().st_size:,} bytes)")

# ---------------------------------------------------------------------------
# Step 1: Fraunces variable TTF
# ---------------------------------------------------------------------------
GOOGLE_FONTS_RAW = "https://raw.githubusercontent.com/google/fonts/main"

print("\n=== Fraunces ===")

FRAUNCES_TTF_NAME = "Fraunces%5BSOFT%2CWONK%2Copsz%2Cwght%5D.ttf"
FRAUNCES_TTF_URL  = f"{GOOGLE_FONTS_RAW}/ofl/fraunces/{FRAUNCES_TTF_NAME}"
FRAUNCES_OFL_URL  = f"{GOOGLE_FONTS_RAW}/ofl/fraunces/OFL.txt"

fraunces_ttf = FRAUNCES_DIR / "fraunces-var.ttf"
fraunces_woff2 = FRAUNCES_DIR / "fraunces-var.woff2"
fraunces_ofl = FRAUNCES_DIR / "OFL.txt"

print(f"  downloading {FRAUNCES_TTF_URL}")
save(fraunces_ttf, fetch(FRAUNCES_TTF_URL))
save(fraunces_ofl, fetch(FRAUNCES_OFL_URL))

print("  subsetting Fraunces variable -> woff2 ...")
subset_to_woff2(fraunces_ttf, fraunces_woff2, LATIN_UNICODES)
fraunces_ttf.unlink()  # remove the raw TTF; keep only the woff2

# ---------------------------------------------------------------------------
# Step 2: Spectral statics
# ---------------------------------------------------------------------------
print("\n=== Spectral ===")

SPECTRAL_FILES = {
    "regular":   "Spectral-Regular.ttf",
    "italic":    "Spectral-Italic.ttf",
    "semibold":  "Spectral-SemiBold.ttf",
}
SPECTRAL_OFL_URL = f"{GOOGLE_FONTS_RAW}/ofl/spectral/OFL.txt"
save(SPECTRAL_DIR / "OFL.txt", fetch(SPECTRAL_OFL_URL))

spectral_woff2: dict[str, Path] = {}
for key, fname in SPECTRAL_FILES.items():
    url = f"{GOOGLE_FONTS_RAW}/ofl/spectral/{fname}"
    print(f"  downloading {url}")
    raw_path = SPECTRAL_DIR / fname
    save(raw_path, fetch(url))

    out = SPECTRAL_DIR / f"spectral-{key}.woff2"
    print(f"  subsetting {fname} -> woff2 ...")
    subset_to_woff2(raw_path, out, LATIN_UNICODES)
    raw_path.unlink()
    spectral_woff2[key] = out

# ---------------------------------------------------------------------------
# Step 3: Noto Serif SC -- Google Fonts unicode-range slicing
# ---------------------------------------------------------------------------
print("\n=== Noto Serif SC ===")

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
NOTO_CSS_URL = (
    "https://fonts.googleapis.com/css2?"
    "family=Noto+Serif+SC:wght@400;700&display=swap"
)

print(f"  fetching CSS from {NOTO_CSS_URL}")
css_text = fetch(NOTO_CSS_URL, headers={"User-Agent": CHROME_UA}).decode("utf-8")

# Parse @font-face blocks
BLOCK_RE = re.compile(
    r"@font-face\s*\{([^}]+)\}",
    re.DOTALL,
)
PROP_RE = re.compile(r"([\w-]+)\s*:\s*([^;]+);", re.DOTALL)

blocks = BLOCK_RE.findall(css_text)
print(f"  found {len(blocks)} @font-face blocks")

if len(blocks) == 0:
    print("ERROR: no @font-face blocks parsed -- check UA or endpoint response")
    print("Response snippet:", css_text[:500])
    sys.exit(1)

noto_css_blocks: list[str] = []

for idx, block_body in enumerate(blocks):
    props: dict[str, str] = {}
    for m in PROP_RE.finditer(block_body):
        key = m.group(1).strip()
        val = m.group(2).strip()
        props[key] = val

    # Extract the gstatic URL
    src_val = props.get("src", "")
    url_match = re.search(r"url\(([^)]+)\)", src_val)
    if not url_match:
        print(f"  [warn] block {idx}: no url() in src, skipping")
        continue
    gstatic_url = url_match.group(1).strip("'\"")

    weight = props.get("font-weight", "400").strip()
    local_name = f"noto-serif-sc-{weight}-{idx}.woff2"
    local_path = NOTO_DIR / local_name

    print(f"  [{idx:03d}] w={weight} -> {local_name}")
    save(local_path, fetch(gstatic_url))

    # Verify woff2 magic bytes
    magic = local_path.read_bytes()[:4]
    if magic != b"wOF2":
        print(f"  [warn] {local_name} does not start with wOF2 magic (got {magic!r})")

    # Build a local @font-face block
    unicode_range = props.get("unicode-range", "").strip()
    font_display  = props.get("font-display", "swap").strip()
    font_style    = props.get("font-style", "normal").strip()

    css_block = (
        f"@font-face {{\n"
        f"  font-family: 'Noto Serif SC';\n"
        f"  font-style: {font_style};\n"
        f"  font-weight: {weight};\n"
        f"  font-display: swap;\n"
        f"  src: url('../fonts/noto-serif-sc/{local_name}') format('woff2');\n"
        f"  unicode-range: {unicode_range};\n"
        f"}}"
    )
    noto_css_blocks.append(css_block)

# Noto OFL
NOTO_OFL_URL = f"{GOOGLE_FONTS_RAW}/ofl/notoserifsc/OFL.txt"
save(NOTO_DIR / "OFL.txt", fetch(NOTO_OFL_URL))

# ---------------------------------------------------------------------------
# Step 4: Write frontend/css/fonts.css
# ---------------------------------------------------------------------------
print("\n=== Writing fonts.css ===")

FRAUNCES_CSS = """\
/* Fraunces -- variable display face (SOFT WONK opsz wght axes)
   Subsetted to latin (U+0000-00FF + typographic extras), woff2.
   Weight range 100-900 from the single variable file. */
@font-face {
  font-family: 'Fraunces';
  font-style: normal;
  font-weight: 100 900;
  font-display: swap;
  src: url('../fonts/fraunces/fraunces-var.woff2') format('woff2');
}"""

SPECTRAL_CSS = """\
/* Spectral -- serif body face, three statics (subsetted latin woff2).
   Weight 700 requests resolve to the 600 face via CSS font matching;
   no faux-bold needed. */
@font-face {
  font-family: 'Spectral';
  font-style: normal;
  font-weight: 400;
  font-display: swap;
  src: url('../fonts/spectral/spectral-regular.woff2') format('woff2');
}
@font-face {
  font-family: 'Spectral';
  font-style: italic;
  font-weight: 400;
  font-display: swap;
  src: url('../fonts/spectral/spectral-italic.woff2') format('woff2');
}
@font-face {
  font-family: 'Spectral';
  font-style: normal;
  font-weight: 600;
  font-display: swap;
  src: url('../fonts/spectral/spectral-semibold.woff2') format('woff2');
}"""

noto_section = "/* Noto Serif SC -- CJK body face, Google Fonts unicode-range slices.\n" \
               "   Each block covers one unicode slice; woff2 from fonts.gstatic.com,\n" \
               "   now self-hosted under /static/fonts/noto-serif-sc/. */\n"
noto_section += "\n\n".join(noto_css_blocks)

HEADER = """\
/*
 * fonts.css -- self-hosted web fonts for LN Translator.
 *
 * Fraunces (display), Spectral (body serif), Noto Serif SC (CJK body).
 * All files live under frontend/fonts/ as subsetted woff2; zero third-party
 * requests at runtime.
 *
 * Regenerate: python scripts/fetch_fonts.py
 *             (requires: pip install fonttools brotli)
 */
"""

css_content = HEADER + "\n" + FRAUNCES_CSS + "\n\n" + SPECTRAL_CSS + "\n\n" + noto_section + "\n"
CSS_OUT.write_text(css_content, encoding="utf-8")
print(f"  wrote {CSS_OUT.relative_to(REPO)}")

# ---------------------------------------------------------------------------
# Size summary
# ---------------------------------------------------------------------------
print("\n=== Size summary ===")

def dir_size(d: Path) -> tuple[int, int]:
    """Return (total_bytes, file_count) for woff2 files in d."""
    files = list(d.glob("*.woff2"))
    total = sum(f.stat().st_size for f in files)
    return total, len(files)

fr_bytes, fr_n = dir_size(FRAUNCES_DIR)
sp_bytes, sp_n = dir_size(SPECTRAL_DIR)
ns_bytes, ns_n = dir_size(NOTO_DIR)

print(f"  Fraunces:     {fr_n} file(s), {fr_bytes/1024:.1f} KB")
print(f"  Spectral:     {sp_n} file(s), {sp_bytes/1024:.1f} KB")
print(f"  Noto Serif SC:{ns_n} slice(s), {ns_bytes/1024:.1f} KB  ({ns_bytes/1024/1024:.2f} MB)")
print(f"  Total:        {(fr_bytes+sp_bytes+ns_bytes)/1024/1024:.2f} MB")

print("\nDone.")
