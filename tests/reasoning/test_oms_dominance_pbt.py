"""Property-based tests for the OMS dominance direction rule (Task 5.5).

Property 4: OMS dominance determines direction correctly.

The rule under test is :func:`ai_fit.reasoning.sizing.oms_dominance_direction`,
the ±20pp dominance rule from the design's "Dominance rule (OMS direction)"
section. Direction is set only when one lean's share of the directional total
(``too_big_count + too_small_count``) exceeds the other's by at least
:data:`ai_fit.reasoning.sizing.DOMINANCE_GAP_PP` percentage points:

* ``too_big`` dominates by >= 20pp  -> ``-1`` (one half size smaller).
* ``too_small`` dominates by >= 20pp -> ``+1`` (one half size larger).
* neither dominates                 -> ``0`` (keep the Stated_Usual_Size).
* no directional signals (0/0)      -> ``0``.
* any negative count                -> ``ValueError``.

**Validates: Requirements 1.5, 1.6, 1.7**
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ai_fit.reasoning.sizing import DOMINANCE_GAP_PP, oms_dominance_direction


# Non-negative counts across a wide-but-fast range. Shrinking drives toward the
# small boundary cases (0, 1, 2 ...) where the ±20pp edge lives.
_COUNTS = st.integers(min_value=0, max_value=10_000)


def _expected_direction(too_big: int, too_small: int) -> int:
    """Reference implementation of the ±20pp rule, derived straight from the spec.

    Computed independently of the module under test (share gap in percentage
    points) so the property is a genuine cross-check, not a tautology.
    """
    total = too_big + too_small
    if total == 0:
        return 0
    gap_pp = 100.0 * (too_big - too_small) / total  # too_big share minus too_small share
    if gap_pp >= DOMINANCE_GAP_PP:
        return -1  # runs big -> size down (Req 1.5)
    if -gap_pp >= DOMINANCE_GAP_PP:
        return +1  # runs small -> size up (Req 1.6)
    return 0  # neither dominates (Req 1.7)


# ---------------------------------------------------------------------------
# Core property: direction matches the independent ±20pp reference
# ---------------------------------------------------------------------------


@given(too_big=_COUNTS, too_small=_COUNTS)
def test_direction_matches_pp_gap_rule(too_big: int, too_small: int) -> None:
    """Direction always equals the sign implied by the ±20pp share-gap rule."""
    assert oms_dominance_direction(too_big, too_small) == _expected_direction(
        too_big, too_small
    )


@given(too_big=_COUNTS, too_small=_COUNTS)
def test_direction_is_in_valid_set(too_big: int, too_small: int) -> None:
    """The direction is always exactly one of -1, 0, or +1 (Req 1.5-1.7)."""
    assert oms_dominance_direction(too_big, too_small) in (-1, 0, 1)


# ---------------------------------------------------------------------------
# Directional semantics: too_big -> down, too_small -> up (Req 1.5, 1.6)
# ---------------------------------------------------------------------------


@given(too_big=_COUNTS, too_small=_COUNTS)
def test_too_big_lean_never_sizes_up(too_big: int, too_small: int) -> None:
    """When "Too big" is the majority lean the direction is never +1 (size up)."""
    if too_big > too_small:
        assert oms_dominance_direction(too_big, too_small) in (-1, 0)


@given(too_big=_COUNTS, too_small=_COUNTS)
def test_too_small_lean_never_sizes_down(too_big: int, too_small: int) -> None:
    """When "Too small" is the majority lean the direction is never -1 (size down)."""
    if too_small > too_big:
        assert oms_dominance_direction(too_big, too_small) in (1, 0)


@given(count=st.integers(min_value=0, max_value=10_000))
def test_equal_leans_are_neutral(count: int) -> None:
    """Equal counts give a 0pp gap, which can never reach the threshold (Req 1.7)."""
    assert oms_dominance_direction(count, count) == 0


# ---------------------------------------------------------------------------
# Symmetry: swapping the two counts negates the direction
# ---------------------------------------------------------------------------


@given(too_big=_COUNTS, too_small=_COUNTS)
def test_swapping_counts_negates_direction(too_big: int, too_small: int) -> None:
    """``direction(a, b) == -direction(b, a)`` — the rule is anti-symmetric."""
    assert oms_dominance_direction(too_big, too_small) == -oms_dominance_direction(
        too_small, too_big
    )


# ---------------------------------------------------------------------------
# Boundary at exactly 20pp: the threshold is inclusive (>=), so an exact 20pp
# gap sets a direction. Build pairs whose share gap is exactly 20pp.
# ---------------------------------------------------------------------------


@given(unit=st.integers(min_value=1, max_value=2_000))
def test_exactly_twenty_pp_gap_sets_direction(unit: int) -> None:
    """A share gap of exactly 20pp sizes down for too_big (threshold is inclusive).

    With ``too_big = 3*unit`` and ``too_small = 2*unit`` the total is ``5*unit`` and
    the gap is ``100*(3u - 2u)/(5u) = 20``pp exactly, independent of ``unit``.
    """
    too_big = 3 * unit
    too_small = 2 * unit
    assert oms_dominance_direction(too_big, too_small) == -1
    # Symmetric: too_small dominating by exactly 20pp sizes up.
    assert oms_dominance_direction(too_small, too_big) == +1


@given(unit=st.integers(min_value=1, max_value=2_000))
def test_just_under_twenty_pp_gap_is_neutral(unit: int) -> None:
    """A gap strictly below 20pp stays neutral (Req 1.7).

    ``too_big = 2*unit``, ``too_small = 3*unit`` has a 20pp gap; nudging the leans
    together by one directional signal pulls the gap below 20pp. Use shares of
    ``(5u+1, 5u-1)`` out of ``10u`` -> gap ``= 100*2/(10u) = 20/u`` pp, which is
    strictly below 20 for every ``unit >= 2`` and exactly 20 at ``unit == 1``.
    """
    too_big = 5 * unit + 1
    too_small = 5 * unit - 1
    gap_pp = 100.0 * (too_big - too_small) / (too_big + too_small)
    expected = -1 if gap_pp >= DOMINANCE_GAP_PP else 0
    assert oms_dominance_direction(too_big, too_small) == expected


# ---------------------------------------------------------------------------
# Zero / zero and negative-count contract
# ---------------------------------------------------------------------------


def test_zero_zero_is_neutral() -> None:
    """No directional fit signals (0/0) yields a neutral direction."""
    assert oms_dominance_direction(0, 0) == 0


@given(
    neg=st.integers(min_value=-10_000, max_value=-1),
    other=st.integers(min_value=0, max_value=10_000),
)
def test_negative_counts_raise(neg: int, other: int) -> None:
    """Any negative count raises ValueError (either argument position)."""
    with pytest.raises(ValueError):
        oms_dominance_direction(neg, other)
    with pytest.raises(ValueError):
        oms_dominance_direction(other, neg)
