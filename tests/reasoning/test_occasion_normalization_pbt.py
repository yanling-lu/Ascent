"""Property-based test for the Occasion Normalization Layer (Task 3.2).

Feature: ai-fit, Property 10: Occasion normalization is a total mapping with
defined drops.

For any raw occasion token from a profile, catalog, or review source,
normalization yields the documented canonical value for mapped tokens and drops
tokens that have no defined mapping. The canonical set is
``{everyday wear, work, special occasion}``; outputs are de-duplicated; matching
is case- and whitespace-insensitive; and each source uses its own delimiter
(semicolon for profile, pipe for catalog/reviews).

**Validates: Requirements 2.9, 2.10, 3.8**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from ai_fit.reasoning.occasion import (
    CANONICAL_OCCASIONS,
    EVERYDAY_WEAR,
    SPECIAL_OCCASION,
    WORK,
    normalize_catalog_occasions,
    normalize_profile_occasions,
    normalize_review_occasion,
    normalize_review_occasions,
)

# ---------------------------------------------------------------------------
# Reference token vocabularies (derived independently from the design, NOT from
# the module internals, so the property is a genuine cross-check).
# ---------------------------------------------------------------------------

# Profile source: semicolon-delimited (Requirement 2.9).
_PROFILE_KNOWN: dict[str, str] = {
    "Everyday": EVERYDAY_WEAR,
    "School / University": EVERYDAY_WEAR,
    "Work": WORK,
    "Going out": SPECIAL_OCCASION,
    "Special occasions": SPECIAL_OCCASION,
}

# Catalog + review sources: pipe-delimited (Requirements 2.10, 3.8).
_CATALOG_KNOWN: dict[str, str] = {
    "casual": EVERYDAY_WEAR,
    "travel": EVERYDAY_WEAR,
    "work": WORK,
    "night out": SPECIAL_OCCASION,
    "wedding": SPECIAL_OCCASION,
}

# Junk tokens that must never map to anything. Chosen to avoid colliding with any
# known token (and to steer clear of the chosen delimiters).
_JUNK_TOKENS: list[str] = [
    "brunch",
    "gym",
    "hiking",
    "unknown",
    "party time",
    "formal gala",
    "",
    "   ",
    "123",
    "n/a",
]


def _mess_case_and_space(token: str) -> st.SearchStrategy[str]:
    """Return a strategy that re-cases ``token`` and pads it with whitespace.

    Normalization is documented as case-insensitive and whitespace-insensitive, so
    any of these perturbations must leave the mapping result unchanged.
    """
    ws = st.sampled_from(["", " ", "  ", "\t", " \t ", "\n"])
    cased = st.sampled_from([token, token.lower(), token.upper(), token.title()])
    return st.builds(lambda c, lead, trail: f"{lead}{c}{trail}", cased, ws, ws)


# Strategies over a single known/junk token for each source, with messy formatting.
def _known_profile_tokens() -> st.SearchStrategy[str]:
    return st.sampled_from(list(_PROFILE_KNOWN)).flatmap(_mess_case_and_space)


def _known_catalog_tokens() -> st.SearchStrategy[str]:
    return st.sampled_from(list(_CATALOG_KNOWN)).flatmap(_mess_case_and_space)


def _junk_tokens() -> st.SearchStrategy[str]:
    return st.sampled_from(_JUNK_TOKENS).flatmap(_mess_case_and_space)


def _expected_canonical(known_map: dict[str, str], token: str) -> str | None:
    """Independent reference: canonical value for a (possibly messy) token."""
    key = token.strip().lower()
    for raw, canonical in known_map.items():
        if raw.lower() == key:
            return canonical
    return None


# A mixed list of tokens: a blend of known (messy-cased) and junk tokens.
def _mixed_tokens(known_strategy: st.SearchStrategy[str]) -> st.SearchStrategy[list[str]]:
    return st.lists(
        st.one_of(known_strategy, _junk_tokens()),
        min_size=0,
        max_size=12,
    )


# ---------------------------------------------------------------------------
# Shared structural assertions used by every source
# ---------------------------------------------------------------------------


def _assert_subset_and_unique(result: list[str]) -> None:
    """Output is a subset of the canonical set with no duplicates."""
    assert isinstance(result, list)
    for value in result:
        assert value in CANONICAL_OCCASIONS, f"{value!r} not canonical"
    assert len(result) == len(set(result)), f"duplicates in {result!r}"


def _assert_matches_reference(
    result: list[str], tokens: list[str], known_map: dict[str, str]
) -> None:
    """Result equals the de-duplicated, in-first-seen-order mapped known tokens."""
    expected: list[str] = []
    for tok in tokens:
        canonical = _expected_canonical(known_map, tok)
        if canonical is not None and canonical not in expected:
            expected.append(canonical)
    assert result == expected


# ---------------------------------------------------------------------------
# Profile source (semicolon-delimited, Requirement 2.9)
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(_mixed_tokens(_known_profile_tokens()))
def test_profile_normalization_total_mapping(tokens: list[str]) -> None:
    """Feature: ai-fit, Property 10 — profile tokens map or drop; semicolon split."""
    raw = ";".join(tokens)
    result = normalize_profile_occasions(raw)
    _assert_subset_and_unique(result)
    _assert_matches_reference(result, tokens, _PROFILE_KNOWN)


@settings(max_examples=200)
@given(_junk_tokens())
def test_profile_junk_always_dropped(junk: str) -> None:
    """An unmapped profile token always produces an empty result (Req 2.9)."""
    assert normalize_profile_occasions(junk) == []


def test_profile_uses_semicolon_not_pipe() -> None:
    """Profile uses ';'. A pipe-joined string is one unmapped token -> dropped."""
    assert normalize_profile_occasions("Everyday|Work") == []
    assert normalize_profile_occasions("Everyday;Work") == [EVERYDAY_WEAR, WORK]


# ---------------------------------------------------------------------------
# Catalog source (pipe-delimited, Requirement 2.10)
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(_mixed_tokens(_known_catalog_tokens()))
def test_catalog_normalization_total_mapping(tokens: list[str]) -> None:
    """Feature: ai-fit, Property 10 — catalog tokens map or drop; pipe split."""
    raw = "|".join(tokens)
    result = normalize_catalog_occasions(raw)
    _assert_subset_and_unique(result)
    _assert_matches_reference(result, tokens, _CATALOG_KNOWN)


@settings(max_examples=200)
@given(_junk_tokens())
def test_catalog_junk_always_dropped(junk: str) -> None:
    """An unmapped catalog token always produces an empty result (Req 2.10)."""
    assert normalize_catalog_occasions(junk) == []


def test_catalog_uses_pipe_not_semicolon() -> None:
    """Catalog uses '|'. A semicolon-joined string is one unmapped token -> dropped."""
    assert normalize_catalog_occasions("casual;work") == []
    assert normalize_catalog_occasions("casual|work") == [EVERYDAY_WEAR, WORK]


# ---------------------------------------------------------------------------
# Review sources (pipe-delimited collection + single-value, Requirement 3.8)
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(_mixed_tokens(_known_catalog_tokens()))
def test_review_collection_normalization_total_mapping(tokens: list[str]) -> None:
    """Feature: ai-fit, Property 10 — review tokens share catalog vocab; pipe split."""
    raw = "|".join(tokens)
    result = normalize_review_occasions(raw)
    _assert_subset_and_unique(result)
    _assert_matches_reference(result, tokens, _CATALOG_KNOWN)


@settings(max_examples=200)
@given(st.one_of(_known_catalog_tokens(), _junk_tokens()))
def test_review_single_value_maps_or_drops(token: str) -> None:
    """A single review value yields its canonical form or None, never junk (Req 3.8)."""
    result = normalize_review_occasion(token)
    expected = _expected_canonical(_CATALOG_KNOWN, token)
    assert result == expected
    assert result is None or result in CANONICAL_OCCASIONS


@settings(max_examples=200)
@given(_junk_tokens())
def test_review_junk_always_dropped(junk: str) -> None:
    """Unmapped review tokens drop in both the single and collection forms (Req 3.8)."""
    assert normalize_review_occasion(junk) is None
    assert normalize_review_occasions(junk) == []


# ---------------------------------------------------------------------------
# De-duplication across every source (repeat a known token many times)
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(
    token=st.sampled_from(list(_CATALOG_KNOWN)),
    repeats=st.integers(min_value=2, max_value=8),
)
def test_dedup_repeated_known_token(token: str, repeats: int) -> None:
    """Repeating a mapped token many times still yields exactly one canonical value."""
    raw = "|".join([token] * repeats)
    result = normalize_catalog_occasions(raw)
    assert result == [_CATALOG_KNOWN[token]]
