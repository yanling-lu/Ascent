"""Property-based tests for the quality-issue flag predicate (Task 8.5).

The rule under test is :func:`ai_fit.tools.quality_report._is_flagged`, the
Quality_Issue_Report flag criterion from the design's "Quality Issue Report"
section:

    A SKU is flagged iff ``quality_count >= 5`` OR
    (``units_sold >= 30`` AND ``quality_return_rate >= 5%``).

where ``quality_return_rate = quality_count / units_sold`` (and is ``None`` when
``units_sold <= 0``, in which case the rate criterion cannot fire).

Records are built directly as :class:`~ai_fit.data.mappers.QualityReturns`
instances (no DataStore load), so the test exercises the pure predicate across a
wide, boundary-aware input space.

**Validates: Requirements 5.3, 5.10**
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from ai_fit.data.mappers import QualityReturns
from ai_fit.tools.quality_report import (
    QUALITY_COUNT_FLAG_THRESHOLD,
    QUALITY_RATE_THRESHOLD,
    UNITS_SOLD_FLOOR,
    _is_flagged,
)

# The four whitelisted quality reasons that make up a breakdown. The breakdown
# values must sum to quality_count for a record to be internally consistent, so
# the generators below partition quality_count across these labels.
_QUALITY_REASONS = (
    "Defective",
    "Poor quality",
    "Arrived in poor condition",
    "Manufacturer defect",
)


def _expected_flagged(quality_count: int, units_sold: int) -> bool:
    """Reference implementation of the flag criterion, derived from the spec.

    Computed independently of the module under test: the rate is recomputed here
    straight from the two integers rather than read off the record, so the
    property is a genuine cross-check and not a tautology.
    """
    count_flag = quality_count >= QUALITY_COUNT_FLAG_THRESHOLD
    if units_sold > 0:
        rate = quality_count / units_sold
        rate_flag = units_sold >= UNITS_SOLD_FLOOR and rate >= QUALITY_RATE_THRESHOLD
    else:
        # No units sold -> rate is undefined, so the rate criterion cannot fire.
        rate_flag = False
    return count_flag or rate_flag


def _breakdown_for(quality_count: int, split: st.SearchStrategy) -> dict:
    """Build a reason_breakdown dict whose values sum to ``quality_count``.

    ``split`` is drawn as a tuple of four non-negative weights; we distribute
    ``quality_count`` across the four reasons proportionally-ish by simple
    running assignment. Zero-valued reasons are dropped to mirror the real
    mapper, which only keeps reasons that actually occurred.
    """
    weights = split
    total_weight = sum(weights)
    breakdown: dict = {}
    if quality_count == 0 or total_weight == 0:
        return breakdown
    remaining = quality_count
    for i, reason in enumerate(_QUALITY_REASONS):
        if i == len(_QUALITY_REASONS) - 1:
            value = remaining
        else:
            value = remaining * weights[i] // total_weight
            remaining -= value
        if value:
            breakdown[reason] = value
    return breakdown


# A varied QualityReturns strategy: quality_count and units_sold span a range
# that straddles both thresholds, with a consistent per-reason breakdown.
@st.composite
def _quality_returns(draw) -> QualityReturns:
    quality_count = draw(st.integers(min_value=0, max_value=200))
    units_sold = draw(st.integers(min_value=0, max_value=4_000))
    split = draw(
        st.tuples(
            st.integers(min_value=0, max_value=5),
            st.integers(min_value=0, max_value=5),
            st.integers(min_value=0, max_value=5),
            st.integers(min_value=0, max_value=5),
        )
    )
    sku = draw(st.text(min_size=1, max_size=8))
    return QualityReturns(
        sku=sku,
        quality_count=quality_count,
        reason_breakdown=_breakdown_for(quality_count, split),
        units_sold=units_sold,
    )


# ---------------------------------------------------------------------------
# Core property: _is_flagged matches the independent boolean
# ---------------------------------------------------------------------------


@given(record=_quality_returns())
def test_flag_matches_independent_predicate(record: QualityReturns) -> None:
    """``_is_flagged`` always equals the independently computed criterion."""
    assert _is_flagged(record) == _expected_flagged(
        record.quality_count, record.units_sold
    )


# ---------------------------------------------------------------------------
# Below both thresholds a SKU is NEVER flagged (Requirement 5.3).
# ---------------------------------------------------------------------------


@given(
    quality_count=st.integers(min_value=0, max_value=QUALITY_COUNT_FLAG_THRESHOLD - 1),
    units_sold=st.integers(min_value=0, max_value=UNITS_SOLD_FLOOR - 1),
)
def test_below_both_thresholds_never_flagged(
    quality_count: int, units_sold: int
) -> None:
    """count < 5 and units_sold < 30 -> neither criterion can fire, so never flagged.

    With fewer than the floor units sold the rate criterion is gated out entirely,
    and the count is below the count threshold, so the predicate must be False.
    """
    record = QualityReturns(
        sku="S", quality_count=quality_count, units_sold=units_sold
    )
    assert _is_flagged(record) is False


# ---------------------------------------------------------------------------
# Count criterion: quality_count >= 5 is always flagged regardless of units.
# ---------------------------------------------------------------------------


@given(
    quality_count=st.integers(min_value=QUALITY_COUNT_FLAG_THRESHOLD, max_value=500),
    units_sold=st.integers(min_value=0, max_value=4_000),
)
def test_count_at_or_above_threshold_always_flagged(
    quality_count: int, units_sold: int
) -> None:
    """A quality_count at or above 5 flags the SKU irrespective of units_sold."""
    record = QualityReturns(
        sku="S", quality_count=quality_count, units_sold=units_sold
    )
    assert _is_flagged(record) is True


# ---------------------------------------------------------------------------
# Boundary: exactly 5 quality returns (count threshold is inclusive).
# ---------------------------------------------------------------------------


@given(units_sold=st.integers(min_value=0, max_value=4_000))
def test_exactly_five_quality_returns_is_flagged(units_sold: int) -> None:
    """quality_count == 5 is flagged by the count criterion (>= is inclusive)."""
    record = QualityReturns(
        sku="S", quality_count=QUALITY_COUNT_FLAG_THRESHOLD, units_sold=units_sold
    )
    assert _is_flagged(record) is True


def test_four_quality_returns_not_flagged_without_rate() -> None:
    """quality_count == 4 is one below the count threshold; with too few units it is unflagged."""
    record = QualityReturns(sku="S", quality_count=4, units_sold=UNITS_SOLD_FLOOR - 1)
    assert _is_flagged(record) is False


# ---------------------------------------------------------------------------
# Boundary: exactly 30 units sold (units floor is inclusive) with an exact 5% rate.
# ---------------------------------------------------------------------------


def test_exactly_thirty_units_at_the_floor_is_flagged() -> None:
    """units_sold == 30 (exactly the floor) flags via the rate criterion.

    quality_count 2 is below the count threshold, so only the rate path can fire.
    The units floor is inclusive (>= 30) and 2/30 ~= 6.7% >= 5%, so the SKU is
    flagged purely on the units-boundary case.
    """
    record = QualityReturns(sku="S", quality_count=2, units_sold=UNITS_SOLD_FLOOR)
    # 2 < 5 so count criterion is off; 2/30 = 6.7% >= 5% and units == floor -> flagged.
    assert record.quality_return_rate is not None
    assert record.quality_return_rate >= QUALITY_RATE_THRESHOLD
    assert _is_flagged(record) is True


def test_exactly_five_percent_rate_is_flagged() -> None:
    """A rate of exactly 5% with units at/above the floor flags via the rate criterion.

    2 returns over 40 units == exactly 0.05; quality_count 2 is below the count
    threshold, so only the rate path can fire, and the rate threshold is inclusive.
    """
    record = QualityReturns(sku="S", quality_count=2, units_sold=40)
    assert record.quality_return_rate == QUALITY_RATE_THRESHOLD
    assert _is_flagged(record) is True


def test_just_below_five_percent_rate_not_flagged() -> None:
    """A rate just under 5% with sub-threshold count is not flagged.

    1 return over 40 units == 2.5% < 5%, and quality_count 1 < 5, so unflagged
    even though units_sold is well above the floor.
    """
    record = QualityReturns(sku="S", quality_count=1, units_sold=40)
    assert record.quality_return_rate < QUALITY_RATE_THRESHOLD
    assert _is_flagged(record) is False


def test_rate_clears_but_units_below_floor_not_flagged() -> None:
    """A high rate below the units floor does not flag (units gate is required).

    2 returns over 29 units is ~6.9% >= 5%, but units_sold 29 < 30 floor and
    quality_count 2 < 5, so the SKU is not flagged.
    """
    record = QualityReturns(sku="S", quality_count=2, units_sold=UNITS_SOLD_FLOOR - 1)
    assert record.quality_return_rate >= QUALITY_RATE_THRESHOLD
    assert _is_flagged(record) is False


def test_just_below_units_floor_not_flagged() -> None:
    """units_sold == 29 (one below the floor) gates out the rate criterion."""
    record = QualityReturns(sku="S", quality_count=4, units_sold=UNITS_SOLD_FLOOR - 1)
    assert _is_flagged(record) is False
