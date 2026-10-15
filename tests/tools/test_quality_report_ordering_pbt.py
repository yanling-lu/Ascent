"""Property-based tests for Quality_Issue_Report ordering (Task 8.6).

Feature: ai-fit, Property 13: Quality report ordering.

For any set of flagged SKUs, the Quality_Issue_Report SHALL be ordered in
descending Quality_Return_Signal count, with SKUs flagged solely by rate
appearing after all count-flagged SKUs.

The ranking under test is :func:`ai_fit.tools.quality_report._rank_flagged`,
which backs the ``flagged[]`` list of
:func:`ai_fit.tools.quality_report.generate_quality_issue_report`
(design "Quality Issue Report": ranking + empty case). The report is
informational-only: it returns ``{flagged[], none_flagged}`` and takes no
product-modifying action (Requirement 5.7); the empty case sets
``none_flagged=True`` with the explicit :data:`NONE_FLAGGED_MESSAGE`
(Requirement 5.8).

The DataStore-backed full report is exercised on a tiny in-memory store built
by populating ``oms_returns_rows`` + ``oms_orders_rows`` and calling
``_build_indexes()`` — the real ``map_all_quality_returns`` key path runs with
no CSV I/O (``DataStore.load`` is never called).

**Validates: Requirements 5.5, 5.7, 5.8**
"""

from __future__ import annotations

import math
from typing import List

from hypothesis import given
from hypothesis import strategies as st

from ai_fit.data.mappers import QualityReturns
from ai_fit.data.store import DataStore
from ai_fit.tools.quality_report import (
    NONE_FLAGGED_MESSAGE,
    QUALITY_COUNT_FLAG_THRESHOLD,
    QUALITY_RATE_THRESHOLD,
    UNITS_SOLD_FLOOR,
    _is_count_flagged,
    _is_flagged,
    _is_rate_flagged,
    _rank_flagged,
    generate_quality_issue_report,
)

# ---------------------------------------------------------------------------
# Strategies: QualityReturns records drawn from both the count-flagged and the
# rate-only flagged regions so the two ranking groups are both populated.
# ---------------------------------------------------------------------------

# A quality reason label is irrelevant to ranking; keep the breakdown simple so
# the generated records stay cheap while quality_count is the field that matters.
_SKUS = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-", min_size=1, max_size=6
)


def _record(sku: str, quality_count: int, units_sold: int) -> QualityReturns:
    """Build a QualityReturns with a breakdown consistent with ``quality_count``."""
    breakdown = {"Defective": quality_count} if quality_count else {}
    return QualityReturns(
        sku=sku,
        quality_count=quality_count,
        reason_breakdown=breakdown,
        units_sold=units_sold,
    )


@st.composite
def _count_flagged_record(draw) -> QualityReturns:
    """A SKU flagged by the count criterion (quality_count >= threshold)."""
    sku = draw(_SKUS)
    count = draw(st.integers(min_value=QUALITY_COUNT_FLAG_THRESHOLD, max_value=50))
    units = draw(st.integers(min_value=0, max_value=2_000))
    return _record(sku, count, units)


@st.composite
def _rate_only_record(draw) -> QualityReturns:
    """A SKU flagged *solely* by the rate criterion.

    quality_count is kept strictly below the count threshold (so it is not
    count-flagged) while units_sold >= floor and the derived rate >= 5% (so it
    is rate-flagged). Choosing ``units <= count / RATE`` guarantees the rate
    clears the threshold.
    """
    sku = draw(_SKUS)
    # rate = count / units >= RATE  <=>  units <= count / RATE; also units >= floor.
    # A viable window [floor, count/RATE] exists only when count/RATE >= floor, i.e.
    # count >= floor * RATE. Start the count there so the units range is non-empty,
    # and keep it strictly below the count threshold so it never also counts-flags.
    min_count = max(1, math.ceil(UNITS_SOLD_FLOOR * QUALITY_RATE_THRESHOLD))
    count = draw(
        st.integers(min_value=min_count, max_value=QUALITY_COUNT_FLAG_THRESHOLD - 1)
    )
    max_units = int(count / QUALITY_RATE_THRESHOLD)
    units = draw(st.integers(min_value=UNITS_SOLD_FLOOR, max_value=max_units))
    return _record(sku, count, units)


# A record that may or may not be flagged (any region), for mixed-input lists.
@st.composite
def _any_record(draw) -> QualityReturns:
    sku = draw(_SKUS)
    count = draw(st.integers(min_value=0, max_value=50))
    units = draw(st.integers(min_value=0, max_value=2_000))
    return _record(sku, count, units)


# ---------------------------------------------------------------------------
# Core ordering property (Property 13 / Requirement 5.5)
# ---------------------------------------------------------------------------


@given(
    records=st.lists(
        st.one_of(_count_flagged_record(), _rate_only_record()),
        min_size=0,
        max_size=25,
    )
)
def test_count_flagged_group_precedes_rate_only_group(
    records: List[QualityReturns],
) -> None:
    """All count-flagged SKUs appear before any rate-only SKU (Req 5.5)."""
    ranked = _rank_flagged(records)
    groups = [0 if _is_count_flagged(r) else 1 for r in ranked]
    # The group sequence is non-decreasing: every 0 precedes every 1.
    assert groups == sorted(groups)


@given(
    records=st.lists(
        st.one_of(_count_flagged_record(), _rate_only_record()),
        min_size=0,
        max_size=25,
    )
)
def test_descending_by_quality_count_within_count_group(
    records: List[QualityReturns],
) -> None:
    """Count-flagged SKUs are ordered descending by quality_count (Req 5.5)."""
    ranked = _rank_flagged(records)
    count_group = [r for r in ranked if _is_count_flagged(r)]
    counts = [r.quality_count for r in count_group]
    assert counts == sorted(counts, reverse=True)


@given(
    records=st.lists(
        st.one_of(_count_flagged_record(), _rate_only_record()),
        min_size=0,
        max_size=25,
    )
)
def test_stable_sku_tiebreak(records: List[QualityReturns]) -> None:
    """Within equal (group, count) the SKU key breaks ties ascending (Req 5.5).

    This makes the order deterministic regardless of the input order, so two
    permutations of the same multiset rank identically.
    """
    ranked = _rank_flagged(records)
    keys = [(0 if _is_count_flagged(r) else 1, -r.quality_count, r.sku) for r in ranked]
    assert keys == sorted(keys)

    # Determinism: a reversed input yields the same ranked key sequence.
    reversed_keys = [
        (0 if _is_count_flagged(r) else 1, -r.quality_count, r.sku)
        for r in _rank_flagged(list(reversed(records)))
    ]
    assert reversed_keys == keys


@given(
    records=st.lists(
        st.one_of(_count_flagged_record(), _rate_only_record()),
        min_size=0,
        max_size=25,
    )
)
def test_ranking_is_a_permutation(records: List[QualityReturns]) -> None:
    """Ranking neither adds nor drops records — it only reorders them."""
    ranked = _rank_flagged(records)
    assert len(ranked) == len(records)
    before = sorted((r.sku, r.quality_count, r.units_sold) for r in records)
    after = sorted((r.sku, r.quality_count, r.units_sold) for r in ranked)
    assert before == after


# ---------------------------------------------------------------------------
# DataStore-backed full-report shape, informational-only, and the empty case.
# ---------------------------------------------------------------------------


def _store_from(returns_rows: List[dict], orders_rows: List[dict]) -> DataStore:
    """Build an in-memory DataStore from raw rows (no CSV load)."""
    store = DataStore(
        oms_returns_rows=returns_rows,
        oms_orders_rows=orders_rows,
    )
    store._build_indexes()
    return store


@given(
    skus=st.lists(_SKUS, min_size=1, max_size=6, unique=True),
    counts=st.lists(
        st.integers(min_value=QUALITY_COUNT_FLAG_THRESHOLD, max_value=20),
        min_size=1,
        max_size=6,
    ),
)
def test_full_report_shape_and_ordering_over_real_key_path(
    skus: List[str], counts: List[int]
) -> None:
    """The DataStore-backed report is informational-only and correctly ranked.

    Build OMS return rows (one "Defective" row per quality return) plus matching
    order rows, run the real ``map_all_quality_returns`` key path, and assert the
    report shape ``{flagged[], none_flagged}``, no customer identifiers, and the
    descending-count ordering (Req 5.5, 5.7).
    """
    n = min(len(skus), len(counts))
    skus = skus[:n]
    counts = counts[:n]

    returns_rows: List[dict] = []
    orders_rows: List[dict] = []
    for sku, count in zip(skus, counts):
        for _ in range(count):
            returns_rows.append({"sku": sku, "reason": "Defective"})
        # Give each SKU some sales so quality_return_rate is well defined.
        orders_rows.append({"sku": sku, "order_number": f"O-{sku}", "shipped": "100"})

    store = _store_from(returns_rows, orders_rows)
    report = generate_quality_issue_report(store)

    # Shape: informational-only payload, no action keys, no customer identifiers.
    assert set(report).issubset({"flagged", "none_flagged", "message"})
    assert report["none_flagged"] is False
    flagged = report["flagged"]
    assert len(flagged) == n
    for entry in flagged:
        assert set(entry) == {
            "sku",
            "quality_count",
            "units_sold",
            "quality_return_rate",
            "reason_breakdown",
            "flagged_by",
        }
        # No customer-identifying field leaks into the report (Req 5.9).
        assert "customer_uid" not in entry
        assert "cnsmr_prof_sid" not in entry

    # Descending by quality_count; SKU ascending breaks ties (Req 5.5).
    ranked_keys = [(-e["quality_count"], e["sku"]) for e in flagged]
    assert ranked_keys == sorted(ranked_keys)


def test_empty_case_sets_none_flagged_with_explicit_message() -> None:
    """No SKU meets the thresholds -> none_flagged True + explicit message (Req 5.8)."""
    # One SKU with a sub-threshold count and plenty of sales so the rate is tiny:
    # quality_count=1 (< 5), units_sold=1000 -> rate 0.1% (< 5%). Not flagged.
    returns_rows = [{"sku": "SKU1", "reason": "Defective"}]
    orders_rows = [{"sku": "SKU1", "order_number": "O-1", "shipped": "1000"}]
    store = _store_from(returns_rows, orders_rows)

    report = generate_quality_issue_report(store)

    assert report["flagged"] == []
    assert report["none_flagged"] is True
    assert report["message"] == NONE_FLAGGED_MESSAGE


def test_empty_store_is_none_flagged() -> None:
    """An entirely empty store flags nothing and reports the empty case (Req 5.8)."""
    store = _store_from([], [])
    report = generate_quality_issue_report(store)
    assert report["flagged"] == []
    assert report["none_flagged"] is True
    assert report["message"] == NONE_FLAGGED_MESSAGE


# ---------------------------------------------------------------------------
# Sanity: the two generators actually land in the groups they claim to.
# ---------------------------------------------------------------------------


@given(record=_count_flagged_record())
def test_count_flagged_generator_is_count_flagged(record: QualityReturns) -> None:
    """Guard the generator: a count-flagged record really is count-flagged."""
    assert _is_count_flagged(record)
    assert _is_flagged(record)


@given(record=_rate_only_record())
def test_rate_only_generator_is_rate_only(record: QualityReturns) -> None:
    """Guard the generator: a rate-only record is rate-flagged but not count-flagged."""
    assert not _is_count_flagged(record)
    assert _is_rate_flagged(record)
    assert _is_flagged(record)
