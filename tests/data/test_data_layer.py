"""Unit tests for the AI Fit data layer (Task 2.3).

Covers the store (``ai_fit.data.store``) and the mappers (``ai_fit.data.mappers``):

* Protected-attribute stripping — ``is_protected_column`` on age/gender/ethnicity/
  nationality/location-style columns, and that the retained legitimate attributes
  (``shoe_sizing_system`` / ``sizing_system`` / ``gender_category``) survive, plus
  an end-to-end assertion that no protected key reaches any emitted record
  (Requirements 10.2, 10.5, 10.6, 10.7).
* CSV loading + SKU/customer indexing — ``DataStore.load`` from tiny temp CSVs and
  the index lookups (Requirement 9.2).
* ``map_customer_profile`` — fit flags, occasion normalization, and measurement
  gating on ``measurements_completed`` (Requirements 1.11).
* ``map_product_catalog_entry`` — ``found=False`` for an absent SKU, ``fit_guidance``
  normalization to the three canonical directions, and ``offered_sizes`` derivation
  from the SKU's style (Requirement 1.16).
* ``map_sku_fit_signals`` — the OMS<->exchange join and fit-reason classification
  (Requirement 1.1).
* ``map_quality_returns`` — ``units_sold = SUM(Shipped)`` and quality classification
  on the same OMS SKU key (Requirements 5.1, 5.10).

Every test builds a tiny in-memory store (a handful of rows) so the suite runs in
well under a second and never touches the large real CSVs.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from ai_fit.data import mappers
from ai_fit.data.mappers import (
    RUNS_LARGE,
    RUNS_SMALL,
    TRUE_TO_SIZE,
    classify_fit_reason,
    classify_quality_reason,
    map_customer_profile,
    map_product_catalog_entry,
    map_quality_returns,
    map_sku_fit_signals,
    normalize_fit_guidance,
)
from ai_fit.data.store import DataStore, is_protected_column


# ---------------------------------------------------------------------------
# In-memory store helpers
# ---------------------------------------------------------------------------


def _store_from_rows(**rows) -> DataStore:
    """Build a DataStore from already-normalized (lowercased-key) row lists.

    Keys must already be normalized (lowercase, no quotes) exactly as the store's
    ``_read_csv`` would produce, since these bypass CSV parsing. Calls
    ``_build_indexes`` so every lookup works.
    """
    store = DataStore(
        customer_profiles_rows=rows.get("customers", []),
        product_catalog_rows=rows.get("catalog", []),
        oms_returns_rows=rows.get("returns", []),
        oms_orders_rows=rows.get("orders", []),
        instore_exchanges_rows=rows.get("exchanges", []),
        customer_reviews_rows=rows.get("reviews", []),
    )
    store._build_indexes()
    return store


# ===========================================================================
# Protected-attribute stripping (Requirements 10.2, 10.5, 10.6, 10.7)
# ===========================================================================


@pytest.mark.parametrize(
    "header",
    [
        "age",
        "customer_age",
        "gender",
        "ethnicity",
        "nationality",
        "location",
        "city",
        "state",
        "province",
        "country",
        "postal_code",
        "zip",
        "latitude",
        "longitude",
        "date_of_birth",
        "dob",
    ],
)
def test_is_protected_column_true_for_protected(header):
    """Age/gender/ethnicity/nationality/location-style columns are protected."""
    assert is_protected_column(header) is True


@pytest.mark.parametrize(
    "header",
    [
        "shoe_sizing_system",
        "sizing_system",
        "gender_category",
        "sku",
        "usual_shoe_size",
        "fit_return_rate",
        "price_usd",
    ],
)
def test_is_protected_column_false_for_legitimate(header):
    """Retained + ordinary columns are not treated as protected."""
    assert is_protected_column(header) is False


def test_is_protected_column_matches_case_insensitively_and_quoted():
    """Normalization handles quoting/casing/whitespace before matching."""
    assert is_protected_column('"Gender"') is True
    assert is_protected_column("  AGE  ") is True
    assert is_protected_column('"shoe_sizing_system"') is False


def test_gender_category_retained_despite_gender_fragment():
    """gender_category survives even though it contains the 'gender' fragment."""
    assert is_protected_column("gender_category") is False
    assert is_protected_column("gender") is True


def test_protected_columns_stripped_end_to_end(tmp_path: Path):
    """A loaded CSV never materializes a protected column into a row/record."""
    csv_path = tmp_path / "profiles.csv"
    _write_csv(
        csv_path,
        header=[
            "synthetic_customer_id",
            "age",
            "gender",
            "location",
            "shoe_sizing_system",
            "usual_shoe_size",
            "measurements_completed",
        ],
        rows=[["C1", "33", "F", "Montreal", "US", "8", "0"]],
    )
    store = DataStore.load(_single_dataset_paths(customer_profiles=csv_path, tmp=tmp_path))

    row = store.get_customer("C1")
    assert row is not None
    assert "age" not in row
    assert "gender" not in row
    assert "location" not in row
    # Legitimate attributes survive.
    assert row["shoe_sizing_system"] == "US"
    assert row["usual_shoe_size"] == "8"


# ===========================================================================
# CSV loading + indexing (Requirement 9.2)
# ===========================================================================


def _write_csv(path: Path, header, rows) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def _single_dataset_paths(tmp: Path, **overrides) -> dict:
    """Return a full dataset_paths map, writing empty-but-valid CSVs for defaults.

    ``overrides`` supplies real tiny CSVs for the datasets a test cares about; every
    other dataset gets a header-only CSV so ``DataStore.load`` finds a file for all
    six keys and never touches the real data.
    """
    defaults_headers = {
        "customer_profiles": ["synthetic_customer_id"],
        "product_catalog": ["sku", "style", "size"],
        "oms_returns": ["sku", "order_number", "reason"],
        "oms_orders": ["sku", "order_number", "shipped"],
        "instore_exchanges": ["original order number", "exchange sku"],
        "customer_reviews": ["sku", "fit_feedback", "width_feedback", "occasion"],
    }
    paths = {}
    for name, header in defaults_headers.items():
        if name in overrides:
            paths[name] = overrides[name]
        else:
            p = tmp / f"{name}_empty.csv"
            _write_csv(p, header=header, rows=[])
            paths[name] = p
    return paths


def test_load_builds_sku_and_customer_indexes(tmp_path: Path):
    """DataStore.load parses tiny CSVs and indexes by sku and customer id."""
    cust = tmp_path / "cust.csv"
    _write_csv(cust, ["synthetic_customer_id", "usual_shoe_size"], [["C1", "9"], ["C2", "7"]])

    cat = tmp_path / "cat.csv"
    _write_csv(cat, ["sku", "style", "size"], [["SKU-A", "STYLE1", "8"], ["SKU-A", "STYLE1", "9"]])

    orders = tmp_path / "orders.csv"
    _write_csv(orders, ["sku", "order_number", "shipped"], [["SKU-A", "ON1", "3"]])

    store = DataStore.load(
        _single_dataset_paths(
            tmp=tmp_path,
            customer_profiles=cust,
            product_catalog=cat,
            oms_orders=orders,
        )
    )

    assert store.get_customer("C1")["usual_shoe_size"] == "9"
    assert store.get_customer("C2")["usual_shoe_size"] == "7"
    assert store.get_customer("missing") is None

    # SKU index groups the two catalog rows under one sku.
    assert len(store.get_catalog_rows("SKU-A")) == 2
    assert store.get_catalog_rows("absent") == []

    # order index + lookups.
    assert len(store.get_orders("SKU-A")) == 1
    assert len(store.get_orders_by_number("ON1")) == 1


def test_load_raises_for_missing_dataset(tmp_path: Path):
    """A configured-but-absent dataset fails fast with FileNotFoundError."""
    paths = _single_dataset_paths(tmp=tmp_path)
    paths["oms_orders"] = tmp_path / "does_not_exist.csv"
    with pytest.raises(FileNotFoundError):
        DataStore.load(paths)


# ===========================================================================
# map_customer_profile (Requirement 1.11)
# ===========================================================================


def test_map_customer_profile_fit_flags_and_occasions():
    """Fit flags parse from 0/1 cells; occasions normalize to the canonical set."""
    store = _store_from_rows(
        customers=[
            {
                "synthetic_customer_id": "C1",
                "shoe_sizing_system": "US",
                "usual_shoe_size": "8",
                "fit_shoes_often_too_big": "1",
                "fit_shoes_often_too_small": "0",
                "fit_tight_around_toes": "1",
                "comfort_style_priority": "comfort",
                "usual_shopping_occasions": "Everyday; Work; Going out",
                "measurements_completed": "0",
            }
        ]
    )
    profile = map_customer_profile(store, "C1")

    assert profile is not None
    assert profile.sizing_system == "US"
    assert profile.fit_profile.too_big is True
    assert profile.fit_profile.too_small is False
    assert profile.fit_profile.tight_toes is True
    assert profile.comfort_style_priority == "comfort"
    assert set(profile.occasions) == {"everyday wear", "work", "special occasion"}


def test_map_customer_profile_unknown_id_returns_none():
    store = _store_from_rows(customers=[])
    assert map_customer_profile(store, "nope") is None


def test_measurements_gated_off_when_not_completed():
    """Foot measurements are suppressed unless measurements_completed = 1."""
    store = _store_from_rows(
        customers=[
            {
                "synthetic_customer_id": "C1",
                "measurements_completed": "0",
                "foot_length_cm": "26.5",
                "foot_width_cm": "9.8",
            }
        ]
    )
    profile = map_customer_profile(store, "C1")
    assert profile.measurements_completed is False
    assert profile.foot_length_cm is None
    assert profile.foot_width_cm is None


def test_measurements_exposed_when_completed():
    """Foot measurements pass through when measurements_completed = 1."""
    store = _store_from_rows(
        customers=[
            {
                "synthetic_customer_id": "C1",
                "measurements_completed": "1",
                "foot_length_cm": "26.5",
                "foot_width_cm": "9.8",
            }
        ]
    )
    profile = map_customer_profile(store, "C1")
    assert profile.measurements_completed is True
    assert profile.foot_length_cm == pytest.approx(26.5)
    assert profile.foot_width_cm == pytest.approx(9.8)


# ===========================================================================
# fit_guidance normalization (Requirement 1.16)
# ===========================================================================


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Runs small - consider sizing up from your usual size", RUNS_SMALL),
        ("Please size up for this style", RUNS_SMALL),
        ("Runs large - consider sizing down", RUNS_LARGE),
        ("We suggest you size down", RUNS_LARGE),
        ("True to size, order your usual", TRUE_TO_SIZE),
        ("", None),
        ("no guidance available", None),
    ],
)
def test_normalize_fit_guidance(raw, expected):
    assert normalize_fit_guidance(raw) == expected


# ===========================================================================
# map_product_catalog_entry (Requirement 1.16)
# ===========================================================================


def test_catalog_entry_absent_sku_found_false():
    store = _store_from_rows(catalog=[])
    entry = map_product_catalog_entry(store, "GHOST")
    assert entry.found is False
    assert entry.sku == "GHOST"


def test_catalog_entry_offered_sizes_from_style_and_normalization():
    """offered_sizes is the distinct, numerically-sorted size set for the style."""
    store = _store_from_rows(
        catalog=[
            {
                "sku": "SKU-A8",
                "style": "STYLE1",
                "size": "8",
                "category": "boots",
                "price_usd": "120.0",
                "occasions": "work|casual|night out",
                "fit_guidance": "Runs small - size up",
                "fit_return_rate": "0.12",
                "toe_box": "narrow",
                "gender_category": "women",
            },
            {"sku": "SKU-A6", "style": "STYLE1", "size": "6.5"},
            {"sku": "SKU-A10", "style": "STYLE1", "size": "10"},
            # Different style should NOT contribute to offered_sizes.
            {"sku": "SKU-B", "style": "STYLE2", "size": "11"},
        ]
    )
    entry = map_product_catalog_entry(store, "SKU-A8")

    assert entry.found is True
    assert entry.category == "boots"
    assert entry.price_usd == pytest.approx(120.0)
    assert entry.fit_guidance == RUNS_SMALL
    assert entry.toe_box == "narrow"
    assert entry.gender_category == "women"
    assert set(entry.occasions) == {"work", "everyday wear", "special occasion"}
    # Numerically sorted, distinct, only STYLE1 sizes (no "11" from STYLE2).
    assert entry.offered_sizes == ["6.5", "8", "10"]


# ===========================================================================
# map_sku_fit_signals — OMS<->exchange join + reason classification (Req 1.1)
# ===========================================================================


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Too big", "too_big"),
        ("return.page.reasons.Too small", "too_small"),
        ("UNCOMFORTABLE", "uncomfortable"),
        ("Defective", None),
        ("", None),
    ],
)
def test_classify_fit_reason(raw, expected):
    assert classify_fit_reason(raw) == expected


def test_fit_signals_counts_reasons_and_joins_exchanges():
    """Fit reasons are counted and exchanges joined on order_number."""
    store = _store_from_rows(
        catalog=[
            {"sku": "SKU-A", "style": "STYLE1", "size": "8"},
            {"sku": "SKU-A9", "style": "STYLE1", "size": "9"},
        ],
        returns=[
            {"sku": "SKU-A", "order_number": "ON1", "reason": "Too small"},
            {"sku": "SKU-A", "order_number": "ON2", "reason": "return.page.reasons.Too big"},
            {"sku": "SKU-A", "order_number": "ON3", "reason": "Uncomfortable"},
        ],
        exchanges=[
            # Customer returned SKU-A (size 8) and exchanged to SKU-A9 (size 9) -> up.
            {"original order number": "ON1", "exchange sku": "SKU-A9"},
        ],
    )
    signals = map_sku_fit_signals(store, "SKU-A")

    assert signals.too_small_count == 1
    assert signals.too_big_count == 1
    assert signals.uncomfortable_count == 1
    assert signals.fit_total == 3
    assert signals.exchange_count == 1
    assert signals.exchange_direction_hint == "up"
    assert signals.total_count == 4


def test_fit_signals_no_exchange_direction_when_unresolvable():
    """No direction hint when exchange SKU size can't be resolved from catalog."""
    store = _store_from_rows(
        catalog=[{"sku": "SKU-A", "style": "STYLE1", "size": "8"}],
        returns=[{"sku": "SKU-A", "order_number": "ON1", "reason": "Too big"}],
        exchanges=[{"original order number": "ON1", "exchange sku": "UNKNOWN"}],
    )
    signals = map_sku_fit_signals(store, "SKU-A")
    assert signals.exchange_count == 1
    assert signals.exchange_direction_hint is None


# ===========================================================================
# map_quality_returns — units_sold = SUM(Shipped), same OMS key (Req 5.1, 5.10)
# ===========================================================================


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Defective", "Defective"),
        ("Poor quality", "Poor quality"),
        ("Arrived in poor condition", "Arrived in poor condition"),
        ("Manufacturer defect", "Manufacturer defect"),
        ("Too big", None),
        ("", None),
    ],
)
def test_classify_quality_reason(raw, expected):
    assert classify_quality_reason(raw) == expected


def test_quality_returns_units_sold_sum_and_classification():
    """units_sold sums Shipped on the SAME OMS sku key as quality counts."""
    store = _store_from_rows(
        returns=[
            {"sku": "SKU-A", "order_number": "ON1", "reason": "Defective"},
            {"sku": "SKU-A", "order_number": "ON2", "reason": "Poor quality"},
            {"sku": "SKU-A", "order_number": "ON3", "reason": "Too small"},  # fit, not quality
        ],
        orders=[
            {"sku": "SKU-A", "order_number": "ON1", "shipped": "5"},
            {"sku": "SKU-A", "order_number": "ON2", "shipped": "3"},
            {"sku": "SKU-A", "order_number": "ON4", "shipped": "2.0"},  # tolerates "2.0"
        ],
    )
    quality = map_quality_returns(store, "SKU-A")

    assert quality.quality_count == 2
    assert quality.reason_breakdown == {"Defective": 1, "Poor quality": 1}
    assert quality.units_sold == 10  # 5 + 3 + 2
    assert quality.quality_return_rate == pytest.approx(2 / 10)


def test_quality_return_rate_none_without_sales():
    """quality_return_rate is None when no units were sold."""
    store = _store_from_rows(
        returns=[{"sku": "SKU-A", "order_number": "ON1", "reason": "Defective"}],
        orders=[],
    )
    quality = map_quality_returns(store, "SKU-A")
    assert quality.quality_count == 1
    assert quality.units_sold == 0
    assert quality.quality_return_rate is None


def test_map_all_quality_returns_covers_union_of_skus():
    """Every SKU with returns OR orders gets a record (same-key join)."""
    store = _store_from_rows(
        returns=[{"sku": "SKU-A", "order_number": "ON1", "reason": "Defective"}],
        orders=[{"sku": "SKU-B", "order_number": "ON2", "shipped": "4"}],
    )
    records = {q.sku: q for q in mappers.map_all_quality_returns(store)}
    assert set(records) == {"SKU-A", "SKU-B"}
    assert records["SKU-A"].quality_count == 1
    assert records["SKU-A"].units_sold == 0
    assert records["SKU-B"].quality_count == 0
    assert records["SKU-B"].units_sold == 4
