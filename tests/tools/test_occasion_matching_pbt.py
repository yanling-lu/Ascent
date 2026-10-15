"""Property-based tests for ``match_occasion`` (Task 8.9).

Covers the Occasion Matching logic in :mod:`ai_fit.tools.alternatives`, which
compares the customer's canonical occasions against a SKU's suited occasions
(catalog occasions enriched by review occasion cues) as a **set intersection**.

Design reference: "Property 9 — Occasion matching reflects set intersection":
for any customer occasion set and SKU occasion set drawn from the canonical set,
the agent reports a match iff the two sets intersect, and the named matching
occasions equal that intersection.

Properties exercised here:

* The ``matched_occasions`` is exactly the set intersection of the customer's
  canonical occasions and the SKU's suited occasions (catalog + review cues),
  and ``status`` is ``"match"`` iff that intersection is non-empty
  (Requirements 2.3, 2.4).
* ``needs_occasion`` fires exactly when no recognized occasion is available from
  either a direct specification or the profile (Requirement 2.6).
* A direct specification (with at least one recognized value) overrides the
  profile occasions entirely (Requirement 2.5).
* Unrecognized occasion values are reported in ``unrecognized_values`` and
  dropped from the match (Requirement 2.8).
* An absent SKU yields ``sku_not_found`` and a SKU with no occasion data yields
  ``occasion_unavailable`` — neither reports a match/no-match (Requirement 2.7).
* Review occasion cues contribute to the SKU's suited occasions as supporting
  evidence (Requirement 3.5).

**Validates: Requirements 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 3.5**

Performance note: these tests NEVER call ``DataStore.load()`` against the real
CSVs. Each test builds a tiny in-memory :class:`DataStore` by populating the raw
``*_rows`` lists and calling ``_build_indexes()`` directly, matching the shape a
real CSV load would have produced.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from hypothesis import given, settings
from hypothesis import strategies as st

from ai_fit.data.store import DataStore
from ai_fit.reasoning.occasion import (
    EVERYDAY_WEAR,
    SPECIAL_OCCASION,
    WORK,
    CANONICAL_OCCASIONS,
)
from ai_fit.tools.alternatives import match_occasion

# ---------------------------------------------------------------------------
# Canonical <-> raw-token vocabularies
# ---------------------------------------------------------------------------

# The three canonical occasions the whole system normalizes to. ``match_occasion``
# receives **already-canonical** customer occasions (the orchestrator / mapper
# normalizes profile and direct values before calling the tool), so the customer
# side of the test passes canonical strings directly.
CANONICAL = sorted(CANONICAL_OCCASIONS)  # ['everyday wear', 'special occasion', 'work']

# Canonical occasion -> a raw catalog/review token that normalizes back to it.
# Catalog is pipe-delimited; reviews carry a single token from the same vocabulary.
# This drives the SKU side (which match_occasion reads via the catalog/review
# mappers, so it must see raw tokens).
_CANONICAL_TO_CATALOG: Dict[str, str] = {
    EVERYDAY_WEAR: "casual",
    WORK: "work",
    SPECIAL_OCCASION: "night out",
}

# Junk tokens that are not canonical occasions and must be dropped / reported as
# unrecognized rather than ever matching.
_JUNK_TOKENS = ["party", "gym", "", "   ", "formal", "beach", "unknown"]

TEST_SKU = "SKU-OCC"


# ---------------------------------------------------------------------------
# In-memory store builder
# ---------------------------------------------------------------------------


def _build_store(
    *,
    catalog_canonical: Optional[Sequence[str]],
    review_canonical: Sequence[str] = (),
    include_sku: bool = True,
) -> DataStore:
    """Build a tiny store where ``TEST_SKU`` has known catalog + review occasions.

    ``catalog_canonical`` is the set of canonical occasions the catalog row should
    carry (``None`` means the SKU is present but with an empty occasions cell, so
    the catalog contributes nothing). ``review_canonical`` seeds review rows whose
    normalized occasion cues enrich the SKU's suited occasions (Requirement 3.5).
    When ``include_sku`` is ``False`` the SKU is absent from the catalog entirely.
    """
    catalog_rows: List[Dict[str, str]] = []
    if include_sku:
        if catalog_canonical is None:
            occasions_cell = ""
        else:
            occasions_cell = "|".join(
                _CANONICAL_TO_CATALOG[c] for c in catalog_canonical
            )
        catalog_rows.append(
            {
                "sku": TEST_SKU,
                "style": "STYLE-OCC",
                "category": "Boots",
                "price_usd": "120.00",
                "size": "8",
                "occasions": occasions_cell,
                "fit_guidance": "True to size",
            }
        )

    review_rows: List[Dict[str, str]] = [
        {
            "sku": TEST_SKU,
            "fit_feedback": "true_to_size",
            "width_feedback": "normal",
            "occasion": _CANONICAL_TO_CATALOG[c],
        }
        for c in review_canonical
    ]

    store = DataStore(
        product_catalog_rows=catalog_rows,
        customer_reviews_rows=review_rows,
    )
    store._build_indexes()
    return store


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# A subset of the canonical occasions (0-3 distinct values), order-insensitive.
_canonical_subset = st.lists(
    st.sampled_from(CANONICAL), min_size=0, max_size=3, unique=True
)

# A non-empty subset for the SKU side (so there is occasion data to match against).
_canonical_nonempty = st.lists(
    st.sampled_from(CANONICAL), min_size=1, max_size=3, unique=True
)

# Junk tokens mixed in with canonical profile tokens.
_junk_list = st.lists(st.sampled_from(_JUNK_TOKENS), min_size=0, max_size=3)


def _customer_tokens(canonical: Sequence[str], junk: Sequence[str]) -> List[str]:
    """Build a customer occasion list from canonical + junk tokens.

    ``match_occasion`` receives already-canonical customer occasions, so the
    recognized values are passed as their canonical strings; junk tokens are
    passed through untouched so they are reported as unrecognized (Req 2.8).
    """
    return list(canonical) + list(junk)


def _expected_unrecognized(junk: Sequence[str]) -> set:
    """The junk tokens the matcher should surface as unrecognized.

    Empty / whitespace-only tokens are ignored entirely (not reported); every
    other junk token is reported, de-duplicated.
    """
    return {j.strip() for j in junk if j and j.strip()}


# ---------------------------------------------------------------------------
# Property 1: match reflects the set intersection (catalog + cues)
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(
    customer=_canonical_subset,
    catalog=_canonical_subset,
    cues=_canonical_subset,
)
def test_matched_is_intersection_with_catalog_and_cues(customer, catalog, cues):
    """``matched_occasions`` == customer ∩ (catalog ∪ review cues); status tracks it.

    Validates: Requirements 2.3, 2.4, 3.5
    """
    sku_occasions = set(catalog) | set(cues)
    store = _build_store(
        catalog_canonical=catalog if catalog else None,
        review_canonical=cues,
    )

    result = match_occasion(store, TEST_SKU, profile_occasions=list(customer))

    expected_intersection = set(customer) & sku_occasions

    if not customer:
        # No recognized customer occasion -> request one, never a match/no-match.
        assert result["status"] == "needs_occasion"
        return

    if not sku_occasions:
        # SKU present but no occasion data at all -> cannot determine suitability.
        assert result["status"] == "occasion_unavailable"
        assert result["matched_occasions"] == []
        return

    # The named matches equal the set intersection, regardless of ordering.
    assert set(result["matched_occasions"]) == expected_intersection
    # status is "match" iff the intersection is non-empty (Property 9).
    assert (result["status"] == "match") == bool(expected_intersection)
    if not expected_intersection:
        assert result["status"] == "no_match"
    # Named matches are drawn only from the customer's recognized occasions.
    assert set(result["matched_occasions"]).issubset(set(customer))
    # ... and only from the SKU's suited occasions.
    assert set(result["matched_occasions"]).issubset(sku_occasions)


# ---------------------------------------------------------------------------
# Property 2: review cues enrich the suited occasions (Requirement 3.5)
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(cue=st.sampled_from(CANONICAL))
def test_review_cue_can_drive_a_match_when_catalog_lacks_it(cue):
    """A cue not in the catalog still contributes to the match (Requirement 3.5).

    With an empty catalog-occasion cell, a customer asking for ``cue`` matches only
    because the review cue enriches the SKU's suited occasions, and that cue is
    surfaced in ``review_cues_used``.

    Validates: Requirements 3.5, 2.3
    """
    store = _build_store(catalog_canonical=None, review_canonical=[cue])

    result = match_occasion(store, TEST_SKU, profile_occasions=[cue])

    assert result["status"] == "match"
    assert result["matched_occasions"] == [cue]
    assert cue in result["review_cues_used"]

    # With review cues disabled, the same SKU has no occasion data -> unavailable.
    result_no_cues = match_occasion(
        store,
        TEST_SKU,
        profile_occasions=[cue],
        include_review_cues=False,
    )
    assert result_no_cues["status"] == "occasion_unavailable"


# ---------------------------------------------------------------------------
# Property 3: direct occasions override the profile (Requirement 2.5)
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(
    profile=_canonical_subset,
    direct=_canonical_nonempty,
    catalog=_canonical_nonempty,
)
def test_direct_occasions_override_profile(profile, direct, catalog):
    """A recognized direct spec replaces the profile occasions entirely.

    The match is computed against ``direct`` only; ``profile`` is ignored.

    Validates: Requirements 2.5, 2.3, 2.4
    """
    store = _build_store(catalog_canonical=catalog)

    result = match_occasion(
        store,
        TEST_SKU,
        profile_occasions=list(profile),
        direct_occasions=list(direct),
    )

    expected = set(direct) & set(catalog)
    assert set(result["customer_occasions"]) == set(direct)
    assert set(result["matched_occasions"]) == expected
    assert (result["status"] == "match") == bool(expected)


# ---------------------------------------------------------------------------
# Property 4: unrecognized values are reported and dropped (Requirement 2.8)
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(
    canonical=_canonical_subset,
    junk=_junk_list,
    catalog=_canonical_nonempty,
)
def test_unrecognized_values_reported_and_dropped(canonical, junk, catalog):
    """Junk tokens never match; non-empty junk is surfaced as unrecognized.

    Validates: Requirements 2.8, 2.3
    """
    store = _build_store(catalog_canonical=catalog)
    tokens = _customer_tokens(canonical, junk)

    result = match_occasion(store, TEST_SKU, profile_occasions=tokens)

    expected_unrecognized = _expected_unrecognized(junk)
    # Every reported unrecognized value is one of the junk tokens we supplied.
    assert set(result["unrecognized_values"]) == expected_unrecognized

    if not canonical:
        # Only junk (or nothing) recognized -> request an occasion.
        assert result["status"] == "needs_occasion"
    else:
        # The match is driven only by the recognized canonical occasions.
        expected = set(canonical) & set(catalog)
        assert set(result["matched_occasions"]) == expected
        # No junk token ever appears among the named matches.
        assert expected_unrecognized.isdisjoint(set(result["matched_occasions"]))


# ---------------------------------------------------------------------------
# Property 5: needs_occasion fires exactly when no recognized occasion exists
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(junk=_junk_list, catalog=_canonical_nonempty)
def test_needs_occasion_when_no_recognized_occasion(junk, catalog):
    """With no recognized occasion from either source, status is needs_occasion.

    Validates: Requirement 2.6
    """
    store = _build_store(catalog_canonical=catalog)

    result = match_occasion(store, TEST_SKU, profile_occasions=list(junk))

    assert result["status"] == "needs_occasion"
    assert result["matched_occasions"] == []
    assert set(result["unrecognized_values"]) == _expected_unrecognized(junk)


# ---------------------------------------------------------------------------
# Property 6: retrieval / data-absence cases produce no match result
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(customer=_canonical_nonempty)
def test_sku_not_found_produces_no_match(customer):
    """An absent SKU yields ``sku_not_found`` and never a match/no-match.

    Validates: Requirement 2.7
    """
    store = _build_store(catalog_canonical=None, include_sku=False)

    result = match_occasion(store, TEST_SKU, profile_occasions=list(customer))

    assert result["status"] == "sku_not_found"
    assert result["matched_occasions"] == []


@settings(max_examples=100)
@given(customer=_canonical_nonempty)
def test_occasion_unavailable_produces_no_match(customer):
    """A SKU present with no occasion data yields ``occasion_unavailable``.

    Validates: Requirement 2.7
    """
    store = _build_store(catalog_canonical=None, review_canonical=())

    result = match_occasion(store, TEST_SKU, profile_occasions=list(customer))

    assert result["status"] == "occasion_unavailable"
    assert result["matched_occasions"] == []
