"""Occasion Normalization Layer.

A shared, side-effect-free mapping applied to every occasion token regardless of
source, producing the canonical Occasion set ``{everyday wear, work, special
occasion}``. Tokens with no defined mapping are dropped (Requirements 2.9, 2.10,
3.8).

Per the design's Occasion Normalization Layer:

* Profile occasions come from ``usual_shopping_occasions`` and are
  **semicolon-delimited** (e.g. ``"Everyday; Work; Going out"``).
* Catalog occasions are stored **pipe-delimited** (e.g. ``"work|casual|night out"``).
* Review occasion cues share the catalog token vocabulary.

The functions here are pure: they perform no I/O, hold no state, and have no
randomness, so they are safe to import and call from any layer (data mappers,
tools, the validator).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Canonical Occasion set
# ---------------------------------------------------------------------------

EVERYDAY_WEAR = "everyday wear"
WORK = "work"
SPECIAL_OCCASION = "special occasion"

# The complete canonical Occasion set (Glossary: Occasion).
CANONICAL_OCCASIONS: frozenset[str] = frozenset(
    {EVERYDAY_WEAR, WORK, SPECIAL_OCCASION}
)

# ---------------------------------------------------------------------------
# Source token -> canonical mappings
# ---------------------------------------------------------------------------

# Profile tokens from ``usual_shopping_occasions`` (Requirement 2.9).
#   "Everyday", "School / University" -> everyday wear
#   "Work"                            -> work
#   "Going out", "Special occasions"  -> special occasion
_PROFILE_MAP: dict[str, str] = {
    "everyday": EVERYDAY_WEAR,
    "school / university": EVERYDAY_WEAR,
    "work": WORK,
    "going out": SPECIAL_OCCASION,
    "special occasions": SPECIAL_OCCASION,
}

# Catalog tokens (Requirement 2.10) and review tokens (Requirement 3.8) share the
# same vocabulary and mapping:
#   "casual", "travel"      -> everyday wear
#   "work"                  -> work
#   "night out", "wedding"  -> special occasion
_CATALOG_MAP: dict[str, str] = {
    "casual": EVERYDAY_WEAR,
    "travel": EVERYDAY_WEAR,
    "work": WORK,
    "night out": SPECIAL_OCCASION,
    "wedding": SPECIAL_OCCASION,
}

# Reviews use the catalog vocabulary (Requirement 3.8).
_REVIEW_MAP: dict[str, str] = dict(_CATALOG_MAP)

# Delimiters per source.
_PROFILE_DELIMITER = ";"
_CATALOG_DELIMITER = "|"


def _normalize_token(raw: str, mapping: dict[str, str]) -> str | None:
    """Map a single raw token to its canonical value, or ``None`` if unmapped.

    Matching is case-insensitive and ignores surrounding whitespace so that minor
    formatting differences in the source data do not cause an otherwise-valid token
    to be dropped. A token that has no entry in ``mapping`` yields ``None``, which
    callers drop (Requirements 2.9, 2.10, 3.8).
    """
    if raw is None:
        return None
    return mapping.get(raw.strip().lower())


def _normalize_delimited(
    raw: str | None, delimiter: str, mapping: dict[str, str]
) -> list[str]:
    """Split a delimited source string, map each token, and drop unmapped ones.

    Returns the canonical values in first-seen order with duplicates removed. An
    empty, whitespace-only, or ``None`` input yields an empty list.
    """
    if not raw:
        return []

    seen: dict[str, None] = {}
    for part in raw.split(delimiter):
        canonical = _normalize_token(part, mapping)
        if canonical is not None and canonical not in seen:
            seen[canonical] = None
    return list(seen)


def normalize_profile_occasions(raw: str | None) -> list[str]:
    """Normalize a profile ``usual_shopping_occasions`` string.

    The input is semicolon-delimited. Each token is mapped per Requirement 2.9 and
    unmapped tokens are dropped. The result is the de-duplicated canonical set in
    first-seen order.
    """
    return _normalize_delimited(raw, _PROFILE_DELIMITER, _PROFILE_MAP)


def normalize_catalog_occasions(raw: str | None) -> list[str]:
    """Normalize a catalog pipe-delimited occasions string.

    Each token is mapped per Requirement 2.10 and unmapped tokens are dropped. The
    result is the de-duplicated canonical set in first-seen order.
    """
    return _normalize_delimited(raw, _CATALOG_DELIMITER, _CATALOG_MAP)


def normalize_review_occasion(raw: str | None) -> str | None:
    """Normalize a single review ``occasion`` value to its canonical form.

    Review cues carry a single occasion value per review, so this maps one token
    (Requirement 3.8). Returns ``None`` for an unmapped, empty, or ``None`` value,
    which the caller ignores.
    """
    return _normalize_token(raw, _REVIEW_MAP) if raw else None


def normalize_review_occasions(raw: str | None) -> list[str]:
    """Normalize a pipe-delimited collection of review occasion values.

    Provided for symmetry with the catalog case when a review source carries more
    than one occasion value in a single pipe-delimited field. Unmapped tokens are
    dropped (Requirement 3.8); the result is de-duplicated in first-seen order.
    """
    return _normalize_delimited(raw, _CATALOG_DELIMITER, _REVIEW_MAP)
