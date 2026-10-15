"""Property-based test for offered-range membership of ``clamp_to_offered``.

Feature: ai-fit, Property 8: Recommended size stays within the offered range

Design "Mechanical post-steps" step 1 and Guardrail Validator Invariant 6
(Requirement 1.8): the Recommended_Size is snapped to the SKU's ``offered_sizes``
by ``clamp_to_offered``. For any proposed size and any non-empty set of offered
sizes, the clamped result SHALL be a member of the offered set and SHALL be the
nearest offered size to the proposed size, with ties resolving to the smaller
offered size so the result is deterministic. Clamping a size that is already
offered returns that size unchanged, and an empty offered set is rejected
(``ValueError``).

**Validates: Requirements 1.8**
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ai_fit.reasoning.sizing import clamp_to_offered

# The canonical half-size grid a real SKU offers (US sizes). Used to build
# realistic offered-size lists that lie on the 0.5 grid.
_HALF_SIZE_GRID = [
    3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0, 6.5, 7.0, 7.5,
    8.0, 8.5, 9.0, 9.5, 10.0, 10.5, 11.0, 11.5, 12.0,
]

# Non-empty offered-size lists drawn from the half-size grid. ``unique`` keeps the
# membership assertion crisp; duplicates are covered separately below.
_OFFERED_HALF_GRID = st.lists(
    st.sampled_from(_HALF_SIZE_GRID), min_size=1, max_size=12, unique=True
)

# Non-empty offered-size lists of arbitrary finite floats — stresses the nearest
# and tie-break logic off the half-size grid. Duplicates are tolerated by the
# implementation, so uniqueness is not required here.
_OFFERED_ARBITRARY = st.lists(
    st.floats(min_value=-50.0, max_value=50.0, allow_nan=False, allow_infinity=False),
    min_size=1,
    max_size=12,
)

# Either flavor of non-empty offered list.
_OFFERED_SIZES = st.one_of(_OFFERED_HALF_GRID, _OFFERED_ARBITRARY)

# Arbitrary target size spanning below, within, and above typical offered ranges.
_TARGET_SIZE = st.floats(
    min_value=-60.0, max_value=60.0, allow_nan=False, allow_infinity=False
)


def _expected_nearest_smaller_tie(size: float, offered: list[float]) -> float:
    """Independent reference: nearest offered size, ties -> smaller size.

    Deliberately computed differently from the implementation (explicit scan over
    the sorted unique candidates rather than a ``min`` with a composite key) so the
    test does not merely restate the code under test.
    """
    candidates = sorted(set(offered))
    best = candidates[0]
    best_dist = abs(best - size)
    for candidate in candidates[1:]:
        dist = abs(candidate - size)
        # Strictly closer wins; an equal distance keeps the earlier (smaller)
        # candidate, which is the smaller-size tie-break.
        if dist < best_dist:
            best = candidate
            best_dist = dist
    return best


@settings(max_examples=200)
@given(size=_TARGET_SIZE, offered_sizes=_OFFERED_SIZES)
def test_result_is_a_member_of_offered_sizes(size, offered_sizes):
    """The clamped size is always one of the offered sizes."""
    result = clamp_to_offered(size, offered_sizes)
    assert result in offered_sizes


@settings(max_examples=200)
@given(size=_TARGET_SIZE, offered_sizes=_OFFERED_SIZES)
def test_result_is_nearest_with_smaller_tie(size, offered_sizes):
    """The clamped size is the nearest offered size; ties resolve to the smaller.

    Verified against an independent reference computation.
    """
    result = clamp_to_offered(size, offered_sizes)
    expected = _expected_nearest_smaller_tie(size, offered_sizes)
    assert result == expected

    # Cross-check the defining property directly: no offered size is strictly
    # closer than the result, and no equally-close offered size is smaller.
    result_dist = abs(result - size)
    for offered in offered_sizes:
        offered_dist = abs(offered - size)
        assert offered_dist >= result_dist or offered == result
        if offered_dist == result_dist:
            assert offered >= result


@settings(max_examples=200)
@given(
    offered_sizes=st.lists(
        st.sampled_from(_HALF_SIZE_GRID), min_size=1, max_size=12, unique=True
    ),
    index=st.integers(min_value=0, max_value=11),
)
def test_already_offered_size_returns_itself(offered_sizes, index):
    """Clamping a size that is already offered returns that size unchanged."""
    target = offered_sizes[index % len(offered_sizes)]
    assert clamp_to_offered(target, offered_sizes) == target


@settings(max_examples=100)
@given(
    size=_TARGET_SIZE,
    offered_sizes=st.lists(
        st.sampled_from(_HALF_SIZE_GRID), min_size=1, max_size=8, unique=True
    ),
)
def test_duplicates_do_not_change_result(size, offered_sizes):
    """Duplicated offered sizes are tolerated and do not change the result."""
    duplicated = offered_sizes + offered_sizes
    assert clamp_to_offered(size, duplicated) == clamp_to_offered(size, offered_sizes)


@settings(max_examples=50)
@given(size=_TARGET_SIZE)
def test_empty_offered_sizes_raises_value_error(size):
    """An empty offered set cannot be recommended and raises ValueError."""
    with pytest.raises(ValueError):
        clamp_to_offered(size, [])
