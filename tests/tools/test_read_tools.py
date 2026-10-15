"""Unit tests for the read Agent Tools (Task 7.3).

Covers the six read tools in :mod:`ai_fit.tools.read_core` and
:mod:`ai_fit.tools.read_signals`:

* ``get_sku_fit_signals``             (tool #1)
* ``get_customer_profile``            (tool #2)
* ``get_product_catalog_entry``       (tool #3)
* ``get_sku_reviews``                 (tool #4)
* ``get_recommendation_outcomes``     (tool #5, via the ``OutcomeReader`` protocol)
* ``get_quality_returns_and_units``   (tool #6)

The assertions pin the exact output shapes from the design's Agent Tool Catalog,
confirm the customer-profile tool is protected-attribute-free, exercise the
empty / absent-SKU and not-found paths, and verify every tool output is
JSON-serializable.

Maps to Requirements 1.1, 1.3, 2.1, 2.2, 3.1, 3.3, 4.3, 4.4, 5.1, 8.1, 8.5, 8.6,
10.1, 10.7.

Performance note: these tests NEVER call ``DataStore.load()`` against the real
CSVs (that reads ~1M rows and takes ~90s). Instead each test builds a tiny
in-memory :class:`DataStore` by populating the raw ``*_rows`` lists and calling
``_build_indexes()`` directly, so every test runs in well under a second.
"""

from __future__ import annotations

import json
from typing import Dict, List

import pytest

from ai_fit.data.store import DataStore
from ai_fit.tools.read_core import (
    get_customer_profile,
    get_product_catalog_entry,
    get_sku_fit_signals,
)
from ai_fit.tools.read_signals import (
    InMemoryOutcomeReader,
    get_quality_returns_and_units,
    get_recommendation_outcomes,
    get_sku_reviews,
)

# A set of protected-attribute column fragments that must never surface in the
# customer-profile tool output (Requirements 10.1, 10.6).
_PROTECTED_FRAGMENTS = (
    "age",
    "birth",
    "dob",
    "gender",  # note: product gender_category is retained, but the profile has none
    "sex",
    "ethnicity",
    "race",
    "nationality",
    "country",
    "location",
    "address",
    "city",
    "state",
    "zip",
    "postal",
)


# ---------------------------------------------------------------------------
# In-memory fixture — a tiny DataStore built without touching the real CSVs
# ---------------------------------------------------------------------------


def _build_store(
    *,
    customers: List[Dict[str, str]] | None = None,
    catalog: List[Dict[str, str]] | None = None,
    returns: List[Dict[str, str]] | None = None,
    orders: List[Dict[str, str]] | None = None,
    exchanges: List[Dict[str, str]] | None = None,
    reviews: List[Dict[str, str]] | None = None,
) -> DataStore:
    """Construct a DataStore from raw rows and build its indexes in-memory.

    Rows use the store's normalized (lower-case) column names, matching what
    ``_strip_protected`` would have produced from a real CSV. Calling
    ``_build_indexes`` is what ``DataStore.load`` does after reading the CSVs, so
    the resulting store behaves identically to a loaded one for the read tools.
    """
    store = DataStore(
        customer_profiles_rows=customers or [],
        product_catalog_rows=catalog or [],
        oms_returns_rows=returns or [],
        oms_orders_rows=orders or [],
        instore_exchanges_rows=exchanges or [],
        customer_reviews_rows=reviews or [],
    )
    store._build_indexes()
    return store


@pytest.fixture
def store() -> DataStore:
    """A small, representative store covering every read tool's inputs.

    SKU ``SKU-1`` has OMS fit returns, a matched size-up exchange, reviews, orders,
    and a quality return. SKU ``SKU-2`` is a true-to-size catalog entry with sales
    but no returns. ``CUST-1`` is a measured profile; ``CUST-2`` is unmeasured.
    """
    catalog = [
        # SKU-1 — style ST-1 has two offered sizes (8 and 9).
        {
            "sku": "SKU-1",
            "style": "ST-1",
            "size": "8",
            "category": "Boots",
            "price_usd": "120.00",
            "occasions": "work|night out",
            "fit_guidance": "Runs small - consider sizing up",
            "fit_return_rate": "0.12",
            "toe_box": "narrow",
            "gender_category": "Women",
        },
        {
            "sku": "SKU-1B",
            "style": "ST-1",
            "size": "9",
            "category": "Boots",
            "price_usd": "120.00",
            "occasions": "work",
            "fit_guidance": "Runs small",
            "fit_return_rate": "0.12",
            "toe_box": "narrow",
            "gender_category": "Women",
        },
        # SKU-2 — true to size, sales but no returns.
        {
            "sku": "SKU-2",
            "style": "ST-2",
            "size": "7",
            "category": "Sneakers",
            "price_usd": "85.50",
            "occasions": "casual|travel",
            "fit_guidance": "True to size",
            "fit_return_rate": "0.03",
            "toe_box": "regular",
            "gender_category": "Men",
        },
    ]
    returns = [
        {"sku": "SKU-1", "order_number": "ORD-1", "reason": "Too small"},
        {
            "sku": "SKU-1",
            "order_number": "ORD-2",
            "reason": "return.page.reasons.Too small",
        },
        {"sku": "SKU-1", "order_number": "ORD-3", "reason": "Uncomfortable"},
        # A quality return on SKU-1 (counts for the quality tool, not fit).
        {"sku": "SKU-1", "order_number": "ORD-4", "reason": "Defective"},
    ]
    exchanges = [
        # ORD-1 was exchanged into SKU-1B (size 9 > size 8) => sized "up".
        {"original order number": "ORD-1", "exchange sku": "SKU-1B"},
    ]
    orders = [
        {"sku": "SKU-1", "order_number": "ORD-1", "shipped": "10"},
        {"sku": "SKU-1", "order_number": "ORD-5", "shipped": "15"},
        {"sku": "SKU-2", "order_number": "ORD-6", "shipped": "40"},
    ]
    reviews = [
        {"sku": "SKU-1", "fit_feedback": "runs_small", "width_feedback": "narrow",
         "occasion": "work"},
        {"sku": "SKU-1", "fit_feedback": "runs_small", "width_feedback": "normal",
         "occasion": "night out"},
        {"sku": "SKU-1", "fit_feedback": "true_to_size", "width_feedback": "narrow",
         "occasion": "work"},
    ]
    customers = [
        {
            "synthetic_customer_id": "CUST-1",
            "shoe_sizing_system": "US",
            "usual_shoe_size": "8",
            "fit_shoes_often_too_big": "0",
            "fit_shoes_often_too_small": "1",
            "fit_shoes_too_narrow": "0",
            "fit_shoes_too_wide": "0",
            "fit_tight_around_toes": "1",
            "fit_heels_slip": "0",
            "fit_no_fit_issues": "0",
            "fit_not_sure": "0",
            "comfort_style_priority": "comfort",
            "usual_shopping_occasions": "Work; Going out",
            "measurements_completed": "1",
            "foot_length_cm": "25.4",
            "foot_width_cm": "9.8",
        },
        {
            "synthetic_customer_id": "CUST-2",
            "shoe_sizing_system": "EU",
            "usual_shoe_size": "41",
            "fit_shoes_often_too_big": "0",
            "fit_shoes_often_too_small": "0",
            "fit_shoes_too_narrow": "0",
            "fit_shoes_too_wide": "0",
            "fit_tight_around_toes": "0",
            "fit_heels_slip": "0",
            "fit_no_fit_issues": "1",
            "fit_not_sure": "0",
            "comfort_style_priority": "",
            "usual_shopping_occasions": "Everyday",
            # measurements NOT completed — foot keys must be omitted even though
            # raw cells carry values.
            "measurements_completed": "0",
            "foot_length_cm": "26.0",
            "foot_width_cm": "10.0",
        },
    ]
    return _build_store(
        customers=customers,
        catalog=catalog,
        returns=returns,
        orders=orders,
        exchanges=exchanges,
        reviews=reviews,
    )


def _assert_json_serializable(value: object) -> None:
    """Round-trip through JSON to prove the tool output is JSON-serializable."""
    assert json.loads(json.dumps(value)) == value


# ---------------------------------------------------------------------------
# Tool #1 — get_sku_fit_signals (Requirements 1.1, 1.3)
# ---------------------------------------------------------------------------


def test_fit_signals_shape_and_counts(store: DataStore) -> None:
    """SKU with returns + a size-up exchange reports the exact catalog shape."""
    result = get_sku_fit_signals(store, "SKU-1")

    assert set(result) == {
        "too_big_count",
        "too_small_count",
        "uncomfortable_count",
        "exchange_count",
        "total_signal_count",
        "exchange_direction_hint",
    }
    assert result["too_big_count"] == 0
    assert result["too_small_count"] == 2  # "Too small" + quoting variant
    assert result["uncomfortable_count"] == 1
    assert result["exchange_count"] == 1  # ORD-1 matched one exchange
    # total = fit signals (3) + exchanges (1); the quality "Defective" return is
    # not a fit signal and is excluded.
    assert result["total_signal_count"] == 4
    assert result["exchange_direction_hint"] == "up"  # SKU-1B size 9 > SKU-1 size 8
    _assert_json_serializable(result)


def test_fit_signals_absent_sku_is_all_zero(store: DataStore) -> None:
    """An absent SKU yields all-zero counts and a null direction hint (sparse OMS)."""
    result = get_sku_fit_signals(store, "SKU-UNKNOWN")

    assert result == {
        "too_big_count": 0,
        "too_small_count": 0,
        "uncomfortable_count": 0,
        "exchange_count": 0,
        "total_signal_count": 0,
        "exchange_direction_hint": None,
    }
    _assert_json_serializable(result)


# ---------------------------------------------------------------------------
# Tool #2 — get_customer_profile (Requirements 2.1, 10.1, 10.7)
# ---------------------------------------------------------------------------


def test_customer_profile_measured_shape(store: DataStore) -> None:
    """A measured profile carries foot measurements and the full catalog shape."""
    result = get_customer_profile(store, "CUST-1")

    assert result["found"] is True
    assert result["sizing_system"] == "US"
    assert result["usual_shoe_size"] == "8"
    assert result["comfort_style_priority"] == "comfort"
    assert result["occasions"] == ["work", "special occasion"]
    assert result["foot_length_cm"] == 25.4
    assert result["foot_width_cm"] == 9.8

    fit = result["fit_profile"]
    assert set(fit) == {
        "too_big",
        "too_small",
        "too_narrow",
        "too_wide",
        "tight_toes",
        "heels_slip",
        "no_issues",
        "not_sure",
    }
    assert fit["too_small"] is True
    assert fit["tight_toes"] is True
    assert fit["too_big"] is False
    _assert_json_serializable(result)


def test_customer_profile_unmeasured_omits_foot_keys(store: DataStore) -> None:
    """An unmeasured profile omits foot keys and null comfort priority (not '')."""
    result = get_customer_profile(store, "CUST-2")

    assert result["found"] is True
    assert "foot_length_cm" not in result
    assert "foot_width_cm" not in result
    assert result["comfort_style_priority"] is None
    assert result["occasions"] == ["everyday wear"]
    assert result["sizing_system"] == "EU"
    _assert_json_serializable(result)


def test_customer_profile_is_protected_attribute_free(store: DataStore) -> None:
    """No protected-attribute keys appear anywhere in the profile output (10.1)."""
    result = get_customer_profile(store, "CUST-1")

    # Flatten all keys (top level + nested fit_profile) and check none contains a
    # protected-attribute fragment.
    keys = list(result.keys()) + list(result["fit_profile"].keys())
    for key in keys:
        for fragment in _PROTECTED_FRAGMENTS:
            assert fragment not in key.lower(), f"protected key leaked: {key}"

    # The internal bookkeeping fields are not part of the model-visible contract.
    assert "synthetic_customer_id" not in result
    assert "measurements_completed" not in result


def test_customer_profile_absent_returns_found_false(store: DataStore) -> None:
    """An unknown customer id returns only {'found': False} (Requirement 8.1)."""
    result = get_customer_profile(store, "NOPE")

    assert result == {"found": False}
    _assert_json_serializable(result)


# ---------------------------------------------------------------------------
# Tool #3 — get_product_catalog_entry (Requirements 2.2, 8.5, 8.6, 10.7)
# ---------------------------------------------------------------------------


def test_catalog_entry_present_shape(store: DataStore) -> None:
    """A present SKU reports the full catalog shape with style-derived sizes."""
    result = get_product_catalog_entry(store, "SKU-1")

    assert result["found"] is True
    assert result["category"] == "Boots"
    assert result["price_usd"] == 120.0
    assert result["occasions"] == ["work", "special occasion"]
    assert result["fit_guidance"] == "runs small"
    assert result["fit_return_rate"] == 0.12
    assert result["toe_box"] == "narrow"
    # offered_sizes is the distinct size range of the SKU's style ST-1 (8 and 9),
    # sorted numerically (Requirements 1.16, 2.2).
    assert result["offered_sizes"] == ["8", "9"]
    # gender_category is a retained legitimate attribute (Requirement 10.7).
    assert result["gender_category"] == "Women"
    _assert_json_serializable(result)


def test_catalog_entry_true_to_size(store: DataStore) -> None:
    """A true-to-size SKU normalizes fit_guidance and reports its own sizes."""
    result = get_product_catalog_entry(store, "SKU-2")

    assert result["found"] is True
    assert result["fit_guidance"] == "true to size"
    assert result["toe_box"] == "regular"
    assert result["offered_sizes"] == ["7"]
    assert result["gender_category"] == "Men"
    _assert_json_serializable(result)


def test_catalog_entry_absent_returns_found_false(store: DataStore) -> None:
    """An absent SKU returns only {'found': False} with no attribute keys (8.6)."""
    result = get_product_catalog_entry(store, "SKU-UNKNOWN")

    assert result == {"found": False}
    _assert_json_serializable(result)


# ---------------------------------------------------------------------------
# Tool #4 — get_sku_reviews (Requirements 3.1, 3.3)
# ---------------------------------------------------------------------------


def test_reviews_shape_and_counts(store: DataStore) -> None:
    """Review tool reports fully-keyed fit/width counts and de-duped cues."""
    result = get_sku_reviews(store, "SKU-1")

    assert set(result) == {
        "review_count",
        "fit_signal_counts",
        "width_signal_counts",
        "occasion_cues",
    }
    assert result["review_count"] == 3

    # Fit/width maps are fully keyed (missing categories zero-filled) so the shape
    # is identical for every SKU.
    assert result["fit_signal_counts"] == {
        "runs_small": 2,
        "runs_large": 0,
        "true_to_size": 1,
    }
    assert result["width_signal_counts"] == {
        "normal": 1,
        "narrow": 2,
        "wide": 0,
    }
    # Occasion cues are canonical and de-duplicated (work seen twice -> once).
    assert result["occasion_cues"] == ["work", "special occasion"]
    _assert_json_serializable(result)


def test_reviews_absent_sku_is_zeroed(store: DataStore) -> None:
    """An absent SKU yields review_count 0, zeroed counts, and empty cues."""
    result = get_sku_reviews(store, "SKU-UNKNOWN")

    assert result == {
        "review_count": 0,
        "fit_signal_counts": {"runs_small": 0, "runs_large": 0, "true_to_size": 0},
        "width_signal_counts": {"normal": 0, "narrow": 0, "wide": 0},
        "occasion_cues": [],
    }
    _assert_json_serializable(result)


# ---------------------------------------------------------------------------
# Tool #5 — get_recommendation_outcomes (Requirements 4.3, 4.4)
# ---------------------------------------------------------------------------


def test_outcomes_shape_and_totals() -> None:
    """Outcome tool reports the four buckets plus a summed outcome_count."""
    reader = InMemoryOutcomeReader(
        {
            "SKU-1": {
                "kept": 5,
                "returned_too_big": 1,
                "returned_too_small": 2,
                "returned_other": 1,
                # An unknown-state count must be ignored (not a resolved signal).
                "unknown": 9,
            }
        }
    )
    result = get_recommendation_outcomes(reader, "SKU-1")

    assert set(result) == {
        "outcome_count",
        "kept",
        "returned_too_big",
        "returned_too_small",
        "returned_other",
    }
    assert result["kept"] == 5
    assert result["returned_too_big"] == 1
    assert result["returned_too_small"] == 2
    assert result["returned_other"] == 1
    # outcome_count sums only the four resolved buckets (unknown excluded).
    assert result["outcome_count"] == 9
    _assert_json_serializable(result)


def test_outcomes_absent_sku_is_all_zero() -> None:
    """A SKU with no outcomes reports all-zero buckets and outcome_count 0."""
    reader = InMemoryOutcomeReader()
    result = get_recommendation_outcomes(reader, "SKU-UNKNOWN")

    assert result == {
        "outcome_count": 0,
        "kept": 0,
        "returned_too_big": 0,
        "returned_too_small": 0,
        "returned_other": 0,
    }
    _assert_json_serializable(result)


# ---------------------------------------------------------------------------
# Tool #6 — get_quality_returns_and_units (Requirements 5.1)
# ---------------------------------------------------------------------------


def test_quality_shape_counts_and_units(store: DataStore) -> None:
    """Quality tool reports per-SKU quality counts + units over the same OMS key."""
    records = get_quality_returns_and_units(store)

    # SKUs returned in sorted order over the union of return/order SKUs.
    by_sku = {record["sku"]: record for record in records}
    assert set(by_sku) == {"SKU-1", "SKU-2"}

    sku1 = by_sku["SKU-1"]
    assert set(sku1) == {"sku", "quality_count", "units_sold", "reason_breakdown"}
    # Only the "Defective" return is a quality reason; the fit returns are excluded.
    assert sku1["quality_count"] == 1
    assert sku1["reason_breakdown"] == {"Defective": 1}
    # units_sold = SUM(Shipped) over SKU-1 orders (10 + 15).
    assert sku1["units_sold"] == 25

    sku2 = by_sku["SKU-2"]
    assert sku2["quality_count"] == 0
    assert sku2["reason_breakdown"] == {}
    assert sku2["units_sold"] == 40

    _assert_json_serializable(records)


def test_quality_empty_store_is_empty_list() -> None:
    """A store with no returns and no orders yields an empty quality report."""
    empty = _build_store()
    assert get_quality_returns_and_units(empty) == []
