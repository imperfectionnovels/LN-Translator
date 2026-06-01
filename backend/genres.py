"""Genre registry.

Each entry names a prose-style profile the translator uses. The genre key is
stored per-novel in `novels.genre`; `build_system_instruction` in
`services/translators/base.py` reads it, loads the matching
`backend/prompts/genres/<overlay>` + `backend/prompts/examples/<key>.md` files,
and composes the per-call system instruction.

Adding a genre is purely additive: write the overlay + examples files under
`backend/prompts/`, then drop a new entry here.

NULL `novels.genre` falls back to `config.DEFAULT_GENRE`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GenreSpec:
    key: str
    name: str
    description: str
    # Path relative to backend/prompts/genres/. The loader appends this to
    # the universal base.md to produce the per-call system instruction.
    #
    # Convention: the matching examples file lives at
    # backend/prompts/examples/<key>.md — `build_system_instruction`
    # reads both. A new GenreSpec MUST ship BOTH files; the existence of
    # the pair is pinned by `tests/test_genre_prompts.py`.
    prompt_overlay: str


GENRES: dict[str, GenreSpec] = {
    "xianxia": GenreSpec(
        key="xianxia",
        name="Xianxia",
        description=(
            "Chinese cultivation fantasy: cultivators, sects, qi, realms, "
            "celestial-tribulation crescendos. Honorifics matter; technique "
            "and realm names are proper nouns."
        ),
        prompt_overlay="xianxia.md",
    ),
    "wuxia": GenreSpec(
        key="wuxia",
        name="Wuxia",
        description=(
            "Chinese martial-arts fiction set in the jianghu. Sworn brotherhood, "
            "sect codes, choreographed combat, internal energy. Grounded "
            "period prose without xianxia cosmic scale."
        ),
        prompt_overlay="wuxia.md",
    ),
    "modern-romance": GenreSpec(
        key="modern-romance",
        name="Modern romance",
        description=(
            "Contemporary romance: urban, office, school, family, celebrity. "
            "Emotional beats drive plot; dialogue and interiority carry the "
            "register. Restrained body language, slow-burn micro-moments."
        ),
        prompt_overlay="modern-romance.md",
    ),
    "isekai": GenreSpec(
        key="isekai",
        name="Isekai",
        description=(
            "Modern protagonist transported to another world. RPG-style "
            "stats and skills, meta-aware interior voice, system "
            "notifications. Modern interior + world-appropriate exterior."
        ),
        prompt_overlay="isekai.md",
    ),
    "slice-of-life": GenreSpec(
        key="slice-of-life",
        name="Slice of life",
        description=(
            "Small-scale, low-stakes everyday fiction. Cafe, cooking, "
            "village, found family. Mood and sensory detail over plot; "
            "restraint over melodrama; observational quiet."
        ),
        prompt_overlay="slice-of-life.md",
    ),
    "mystery": GenreSpec(
        key="mystery",
        name="Mystery",
        description=(
            "Investigation-driven fiction. Whodunit, procedural, thriller, "
            "cozy. Precise clue rendering, witness statements preserved "
            "with their hedges, red herrings rendered straight."
        ),
        prompt_overlay="mystery.md",
    ),
    "litrpg": GenreSpec(
        key="litrpg",
        name="LitRPG",
        description=(
            "System-driven progression fantasy: stat windows, skill trees, "
            "level-ups, quest logs, dungeon raids. Game UI artifacts render "
            "as displayed prose, not paraphrased; numbers stay precise."
        ),
        prompt_overlay="litrpg.md",
    ),
    "sci-fi": GenreSpec(
        key="sci-fi",
        name="Sci-fi",
        description=(
            "Science fiction across the spectrum: hard, space opera, "
            "cyberpunk, mecha, near-future. Technical vocabulary handled "
            "precisely; speculative concepts named consistently."
        ),
        prompt_overlay="sci-fi.md",
    ),
    "fantasy": GenreSpec(
        key="fantasy",
        name="Fantasy",
        description=(
            "Western-style fantasy: kingdoms, dungeons, magic systems, "
            "dragons. Distinct from xianxia/wuxia/isekai — no cultivation, "
            "no transmigrated protagonist, native to the setting."
        ),
        prompt_overlay="fantasy.md",
    ),
    "yuri-bl": GenreSpec(
        key="yuri-bl",
        name="Yuri / Boys-Love",
        description=(
            "Same-sex romance: Japanese yaoi / yuri, Chinese danmei / baihe. "
            "Emotional interiority, slow-burn intimacy, period or modern "
            "settings; restraint over explicitness."
        ),
        prompt_overlay="yuri-bl.md",
    ),
}


def is_known_genre(key: str | None) -> bool:
    """True if `key` is in the registry. NULL is allowed by callers (it
    resolves to DEFAULT_GENRE downstream), so this returns False for NULL."""
    return key is not None and key in GENRES


def normalize_and_validate_genre(genre: str | None) -> str | None:
    """Normalize + validate a user-supplied genre key, raising HTTP 400 on an
    unknown key.

    Empty / None / whitespace-only -> None (no genre selected; the column
    stays NULL and resolves to DEFAULT_GENRE downstream). A non-empty key is
    lower-cased and checked against the registry. This is the single source of
    truth for the import routes (paste/upload/bulk/scrape) and the novel-PATCH
    genre field, so the two trust boundaries can't drift on what they accept.

    Importing HTTPException here keeps both call sites a one-liner; genres.py
    is already a backend-internal module, and the 400 surface is part of the
    validation contract these callers share.
    """
    from fastapi import HTTPException  # noqa: PLC0415

    if genre is None:
        return None
    g = genre.strip().lower()
    if not g:
        return None
    if not is_known_genre(g):
        raise HTTPException(
            status_code=400,
            detail=f"unknown genre {g!r}; see backend/genres.py for valid keys",
        )
    return g


# Internal safety-net key used by resolve_genre when no genre is set on a
# novel and no override is provided. Not user-facing — kept off the GENRES
# registry so the dropdown doesn't list it. The prompts/genres/generic.md
# overlay stays on disk for existing rows that still carry genre='generic'.
_FALLBACK_GENRE = "generic"


def resolve_genre(key: str | None, default: str) -> str:
    """Resolve a possibly-NULL genre to a registry key. Falls back to
    `default` if `key` is NULL or unknown; if `default` is also unknown,
    returns the internal `_FALLBACK_GENRE` so the translator always has
    a prompt overlay to load.

    Legacy DB rows can still carry `genre='generic'` (the value was a
    user-facing option before May 2026). We accept it here even though
    it's no longer in `GENRES`, so the matching overlay file at
    `prompts/genres/generic.md` still loads for those rows. New imports
    can't pick it because the UI dropdown is sourced from `GENRES`."""
    if key and key in GENRES:
        return key
    if key == _FALLBACK_GENRE:
        return key
    if default in GENRES:
        return default
    return _FALLBACK_GENRE
