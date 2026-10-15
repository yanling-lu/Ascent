"""Property-based test for ``find_alternative_styles`` ranking, cap, and gating.

Feature: ai-fit, Property 16: Alternative styles are ranked and capped at three
(Agent Tool Catalog #10; design "Alternatives" ranking rule and availability proxy
= catalog-present).

For a browsed SKU that does not suit the requested occasion, the tool scans the
catalog-present (available) SKUs, keeps those whose suited occasions include the
requested occasion, excludes the browsed SKU, ranks them by category closeness
(same-category group first), then by smallest absolute price difference from the
browsed SKU, with a stable SKU tiebreak, and presents at most the top three. When
1-3 candidates exist all are presented; when none match (including a non-canonical
requested occasion) the result is the explicit none-available case.

**Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.5**
"""

from __future__ import annotations

from typing import List, Optional

from hypothesis import given, settings
from hypothesis import strategies as st

from ai_fit.data.store import DataStore
from ai_fit.reasoning.occasion import (
    EVERYDAY_WEAR,
    SPECIAL_OCCASION,
    WORK,
)
from ai_fit.tools.alternatives import MAX_ALTERNATIVES, find_alternative_styles

# ---------------------------------------------------------------------------
# Catalog vocabulary: raw tokens the Occasion Normalization Layer maps to the
# canonical set, so a generated catalog row produces predictable suited occasions.
# ---------------------------------------------------------------------------

# raw catalog occasion token -> canonical occasion it normalizes to.
_RAW_TO_CANONICAL = {
    "casual": EVERYDAY_WEAR,
    "travel": EVERYDAY_WEAR,
    "work": WORK,
    "night out": SPECIAL_OCCASION,
    "wedding": SPECIAL_OCCASION,
}
_RAW_OCCASION_TOKENS = sorted(_RAW_TO_CANONICAL)
_CANONICAL_OCCASIONS = [EVERYDAY_WEAR, WORK, SPECIAL_OCCASION]

# A small category vocabulary so same-category collisions happen often enough to
# exercise the category-closeness ranking rung.
_CATEGORIES = ["Sneakers", "Boots", "Sandals", "Heels"]


# ---------------------------------------------------------------------------
# Strategies for a single synthetic catalog row.
# ---------------------------------------------------------------------------

# Prices chosen on a coarse grid so equal absolute price distances (which exercise
# the SKU tiebreak) occur with meaningful frequency.
_price_strategy = st.one_of(
    st.sampled_from([20.0, 30.0, 40.0, 50.0, 60.0, 80.0, 100.0]),
    st.none(),  # a missing price must sort last within its category group
)

_raw_occasions_strategy = st.lists(
    st.sampled_from(_RAW_OCCASION_TOKENS), min_size=0, max_size=4, unique=True
)


@st.composite
def _catalog_row(draw, sku: str):
    """Build one raw catalog row dict as the store's loader would hold it."""
    category = draw(st.one_of(st.sampled_from(_CATEGORIES), st.none()))
    price = draw(_price_strategy)
    raw_occasions = draw(_raw_occasions_strategy)
    return {
        "sku": sku,
        # Give each SKU its own style so offered_sizes stays per-SKU and the
        # category/price/occasions projection comes straight from this row.
        "style": f"STYLE-{sku}",
        "category": "" if category is None else category,
        "price_usd": "" if price is None else f"{price}",
        "occasions": "|".join(raw_occasions),
        "size": "8",
    }


@st.composite
def _catalog(draw):
    """Build a small in-memory catalog plus a chosen browsed SKU and occasion.

    Returns ``(store, browsed_sku, requested_occasion)``. SKU keys are zero-padded
    so lexical order is well-defined for the tiebreak reference.
    """
    n = draw(st.integers(min_value=1, max_value=8))
    skus = [f"SKU{i:03d}" for i in range(n)]
    rows = [draw(_catalog_row(sku=sku)) for sku in skus]

    store = DataStore(product_catalog_rows=rows)
    store._build_indexes()

    browsed_sku = draw(st.sampled_from(skus))
    requested_occasion = draw(st.sampled_from(_CANONICAL_OCCASIONS))
    return store, browsed_sku, requested_occasion


# ---------------------------------------------------------------------------
# Independent reference: recompute candidates and ranking from the raw rows,
# deliberately without reusing the tool's helpers.
# ---------------------------------------------------------------------------


def _canonical_occasions_for_row(row: dict) -> List[str]:
    """Canonical suited occasions for a raw catalog row (independent of mappers)."""
    raw = row.get("occasions", "")
    result: List[str] = []
    for token in raw.split("|"):
        canonical = _RAW_TO_CANONICAL.get(token.strip().lower())
        if canonical is not None and canonical not in result:
            result.append(canonical)
    return result


def _price_or_none(row: dict) -> Optional[float]:
    text = row.get("price_usd", "")
    return float(text) if text else None


def _category_or_none(row: dict) -> Optional[str]:
    cat = row.get("category", "")
    return cat or None


def _reference_ranked_skus(
    rows: List[dict], browsed_sku: str, occasion: str
) -> List[str]:
    """Reference ranking: category-closeness, price distance, SKU tiebreak."""
    browsed_row = next(r for r in rows if r["sku"] == browsed_sku)
    browsed_cat = _category_or_none(browsed_row)
    browsed_price = _price_or_none(browsed_row)

    candidates = []
    for row in rows:
        sku = row["sku"]
        if sku == browsed_sku:
            continue
        if occasion not in _canonical_occasions_for_row(row):
            continue
        cat = _category_or_none(row)
        same_category = cat is not None and browsed_cat is not None and cat == browsed_cat
        category_rank = 0 if same_category else 1
        price = _price_or_none(row)
        if price is not None and browsed_price is not None:
            price_distance = abs(price - browsed_price)
        else:
            price_distance = float("inf")
        candidates.append((category_rank, price_distance, sku))

    candidates.sort()
    return [sku for (_, _, sku) in candidates]


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


@settings(max_examples=300)
@given(data=_catalog())
def test_every_alternative_suits_requested_occasion(data):
    """Each returned alternative's occasions include the requested occasion (Req 6.1, 6.4)."""
    store, browsed_sku, occasion = data
    result = find_alternative_styles(store, occasion, browsed_sku)
    assert result["requested_occasion"] == occasion
    for alt in result["alternatives"]:
        assert occasion in alt["occasions"]


@settings(max_examples=300)
@given(data=_catalog())
def test_browsed_sku_never_returned(data):
    """The browsed SKU is never recommended back (Req 6.1)."""
    store, browsed_sku, occasion = data
    result = find_alternative_styles(store, occasion, browsed_sku)
    returned = {alt["sku"] for alt in result["alternatives"]}
    assert browsed_sku not in returned


@settings(max_examples=300)
@given(data=_catalog())
def test_capped_at_three(data):
    """At most three alternatives are ever presented (Req 6.2)."""
    store, browsed_sku, occasion = data
    result = find_alternative_styles(store, occasion, browsed_sku)
    assert len(result["alternatives"]) <= MAX_ALTERNATIVES


@settings(max_examples=400)
@given(data=_catalog())
def test_ranking_matches_reference_and_caps(data):
    """Order and cap match an independent category/price/SKU reference (Req 6.2, 6.3)."""
    store, browsed_sku, occasion = data
    result = find_alternative_styles(store, occasion, browsed_sku)

    expected_order = _reference_ranked_skus(
        store.product_catalog_rows, browsed_sku, occasion
    )
    expected_top = expected_order[:MAX_ALTERNATIVES]

    actual = [alt["sku"] for alt in result["alternatives"]]
    assert actual == expected_top

    # When 1-3 candidates exist, every candidate is returned (Req 6.3).
    if 1 <= len(expected_order) <= MAX_ALTERNATIVES:
        assert actual == expected_order


@settings(max_examples=300)
@given(data=_catalog())
def test_none_available_iff_no_candidates(data):
    """none_available is True with an empty list exactly when no candidate matches (Req 6.5)."""
    store, browsed_sku, occasion = data
    result = find_alternative_styles(store, occasion, browsed_sku)

    expected_order = _reference_ranked_skus(
        store.product_catalog_rows, browsed_sku, occasion
    )
    if expected_order:
        assert result["none_available"] is False
        assert len(result["alternatives"]) >= 1
    else:
        assert result["none_available"] is True
        assert result["alternatives"] == []


@settings(max_examples=200)
@given(
    data=_catalog(),
    bogus=st.sampled_from(["", "party", "gym", "formal", "casual", "night out"]),
)
def test_non_canonical_occasion_yields_none_available(data, bogus):
    """A non-canonical requested occasion yields the none-available result (Req 6.5)."""
    store, browsed_sku, _ = data
    result = find_alternative_styles(store, bogus, browsed_sku)
    assert result["none_available"] is True
    assert result["alternatives"] == []
