"""Pure sizing helpers used by the Guardrail Validator and the reasoning rubric.

Every function in this module is **pure and deterministic**: it performs no I/O,
holds no mutable module state, and uses no randomness. For identical inputs each
helper always returns an identical result, which is what lets the design's
Guardrail Validator satisfy Requirement 10.4 (identical signals -> identical
recommendation).

The module provides four groups of helpers, mirroring the design's
"Sizing Reasoning + Guardrail Validator" section:

1. Measurement lookup (``measurement_lookup``) — maps a measured foot length to the
   nearest offered half size via the design's monotonic half-size table, offsetting
   men's sizing relative to women's (Requirement 1.11, 1.14).
2. Offered-range clamp (``clamp_to_offered``) — snaps a size to the nearest size the
   SKU actually offers (Requirement 1.8).
3. Half-size arithmetic (``half_steps_between`` / ``apply_half_steps``) — the discrete
   half-size step algebra the net-single-half-size cap is expressed in
   (Requirements 1.5, 1.6, 1.12, 1.13, 1.19).
4. OMS dominance (``oms_dominance_direction``) and confidence tiers
   (``base_confidence_tier``, ``adjust_tier``, plus the ``low/medium/high`` <-> index
   mapping) — the ±20pp direction rule and the ordered, clamped confidence arithmetic
   (Requirements 1.3, 1.4, 1.5, 1.6, 1.7, 1.17, 1.18, 1.19, 1.20).
"""

from __future__ import annotations

import math

# ---------------------------------------------------------------------------
# Size grid constants
# ---------------------------------------------------------------------------

# One half size is the atomic step on the US size grid.
HALF_SIZE: float = 0.5

# ---------------------------------------------------------------------------
# Sizing systems
# ---------------------------------------------------------------------------

# The two sizing systems read from the Customer_Profile ``shoe_sizing_system``
# field. ``Sizing_System`` is a sizing-scale attribute (not personal gender) and is
# a legitimate recommendation input (Requirement 1.14, glossary).
WOMENS_SIZING: str = "Women's sizing"
MENS_SIZING: str = "Men's sizing"

# Men's US sizing runs offset from women's US sizing for the same foot length. The
# conventional offset is that a men's size is ~1.5 sizes smaller than the women's
# size for the same last, i.e. women's 7.0 ~= men's 5.5. The measurement table below
# is authored in women's US; a men's lookup subtracts this offset. This is an
# explicit modeling assumption flagged in the design for validation against real
# last/size data.
MENS_OFFSET_FROM_WOMENS: float = 1.5


# ---------------------------------------------------------------------------
# Measurement lookup table (design assumption, to validate)
# ---------------------------------------------------------------------------

# Monotonic half-size table mapping measured foot length (cm) to a women's US half
# size. The design gives the illustrative anchor rows:
#
#   22.0-22.4 cm -> 5.0 ; 22.5-22.9 -> 5.5 ; 23.0-23.4 -> 6.0 ; 23.5-23.9 -> 6.5
#   24.0-24.4   -> 7.0 ; 24.5-24.9 -> 7.5 ; 25.0-25.4 -> 8.0 ; 25.5-25.9 -> 8.5 ...
#
# The relationship is linear: each 0.5 cm band is one half size, anchored at
# 22.0 cm -> women's 5.0. We encode the anchor + step rather than a long literal
# table so the mapping is exactly monotonic and extends smoothly in both
# directions. Lengths below the smallest band clamp to the smallest size; the
# offered-range clamp handles the SKU-specific boundaries afterward.
_ANCHOR_LENGTH_CM: float = 22.0
_ANCHOR_WOMENS_SIZE: float = 5.0
_CM_PER_HALF_SIZE: float = 0.5

# Smallest women's size the raw table resolves to before the offered-range clamp.
# Measurements shorter than the smallest band resolve to this floor; the SKU's
# offered sizes then constrain the final value via ``clamp_to_offered``.
_MIN_TABLE_WOMENS_SIZE: float = 5.0


def _band_floor(value: float) -> int:
    """Return the number of whole half-size bands at or below ``value``.

    The design table buckets foot length into 0.5 cm bands (22.0-22.4 -> 5.0,
    22.5-22.9 -> 5.5, ...), so a length falls into the band it reaches but does not
    exceed. This is a deterministic floor of the (possibly negative) band position.
    """
    return math.floor(value + 1e-9)  # tiny epsilon absorbs float noise at band edges


def measurement_lookup(foot_length_cm: float, sizing_system: str) -> float:
    """Map a measured foot length to the nearest offered half size.

    Uses the design's monotonic half-size table: foot length 22.0 cm anchors to
    women's US 5.0, and every additional 0.5 cm is one half size larger. For men's
    sizing the resolved women's size is offset by :data:`MENS_OFFSET_FROM_WOMENS`
    (men's runs ~1.5 sizes smaller than women's for the same foot length).

    Foot_Measurement is the highest-precision fit signal and may override a size
    derived from SKU-level signals (Requirement 1.11); the Stated_Usual_Size is
    interpreted in the context of the customer's Sizing_System (Requirement 1.14).

    Args:
        foot_length_cm: Measured foot length in centimeters. Must be positive.
        sizing_system: Either :data:`WOMENS_SIZING` or :data:`MENS_SIZING`.

    Returns:
        The half-size value (a float on the 0.5 grid). The result is **not** yet
        clamped to any particular SKU's offered sizes; call
        :func:`clamp_to_offered` for that.

    Raises:
        ValueError: If ``foot_length_cm`` is not positive, or if ``sizing_system``
            is not one of the two recognized systems.
    """
    if not foot_length_cm > 0:
        raise ValueError(f"foot_length_cm must be positive, got {foot_length_cm!r}")
    if sizing_system not in (WOMENS_SIZING, MENS_SIZING):
        raise ValueError(
            f"sizing_system must be {WOMENS_SIZING!r} or {MENS_SIZING!r}, "
            f"got {sizing_system!r}"
        )

    # Which 0.5 cm band the length falls into, relative to the 22.0 cm anchor. The
    # band is floored so e.g. 25.5-25.9 cm all map to the same size (design table).
    bands_above_anchor = _band_floor(
        (foot_length_cm - _ANCHOR_LENGTH_CM) / _CM_PER_HALF_SIZE
    )
    raw_womens_size = _ANCHOR_WOMENS_SIZE + bands_above_anchor * HALF_SIZE

    # Floor to the smallest table size before any SKU-specific clamp.
    womens_size = max(raw_womens_size, _MIN_TABLE_WOMENS_SIZE)

    if sizing_system == MENS_SIZING:
        return womens_size - MENS_OFFSET_FROM_WOMENS
    return womens_size


# ---------------------------------------------------------------------------
# Offered-range clamp
# ---------------------------------------------------------------------------


def clamp_to_offered(size: float, offered_sizes: list[float]) -> float:
    """Snap ``size`` to the nearest size the SKU actually offers.

    If the computed Recommended_Size falls below the smallest offered size or above
    the largest, it is clamped to the nearest offered boundary; otherwise it snaps
    to the closest offered half size (Requirement 1.8).

    Ties (a size exactly between two offered sizes) resolve to the **smaller**
    offered size so the result is deterministic.

    Args:
        size: The proposed size on the half-size grid.
        offered_sizes: The distinct sizes offered for the SKU. Order does not
            matter; duplicates are tolerated.

    Returns:
        The nearest offered size.

    Raises:
        ValueError: If ``offered_sizes`` is empty (a SKU with no offered sizes
            cannot be recommended).
    """
    if not offered_sizes:
        raise ValueError("offered_sizes must not be empty")

    # Nearest by absolute distance; on a tie prefer the smaller size. Sorting makes
    # the tie-break deterministic regardless of input order.
    return min(sorted(offered_sizes), key=lambda offered: (abs(offered - size), offered))


# ---------------------------------------------------------------------------
# Half-size arithmetic
# ---------------------------------------------------------------------------


def half_steps_between(size: float, reference_size: float) -> int:
    """Return the signed number of half-size steps from ``reference_size`` to ``size``.

    One half size = one step. A positive result means ``size`` is larger than
    ``reference_size`` (stepped up); negative means smaller (stepped down). This is
    the integer the net-single-half-size cap is expressed in: the validator rejects
    any non-measurement shift whose magnitude exceeds 1 (Requirements 1.5, 1.6,
    1.12, 1.13, 1.19).

    Args:
        size: The proposed size on the half-size grid.
        reference_size: The baseline size (typically the Stated_Usual_Size).

    Returns:
        The signed step count, rounded to the nearest whole half-size step.
    """
    return round((size - reference_size) / HALF_SIZE)


def apply_half_steps(size: float, steps: int) -> float:
    """Return the size that is ``steps`` half-sizes away from ``size``.

    Positive ``steps`` moves up (larger), negative moves down (smaller). Inverse of
    :func:`half_steps_between`: ``apply_half_steps(ref, half_steps_between(s, ref))``
    reproduces ``s`` when ``s`` lies on the half-size grid relative to ``ref``.

    Args:
        size: The baseline size on the half-size grid.
        steps: Signed number of half-size steps to apply.

    Returns:
        The resulting size on the half-size grid.
    """
    return size + steps * HALF_SIZE


# ---------------------------------------------------------------------------
# OMS dominance (±20pp direction rule)
# ---------------------------------------------------------------------------

# A lean must exceed the other by at least this many percentage points of total
# fit signals before it sets a direction (Requirements 1.5, 1.6, 1.7).
DOMINANCE_GAP_PP: float = 20.0


def oms_dominance_direction(too_big_count: int, too_small_count: int) -> int:
    """Return the half-size direction implied by OMS fit-return lean.

    Direction is set only when one lean exceeds the other by at least
    :data:`DOMINANCE_GAP_PP` percentage points of total fit signals:

    * ``too_big`` dominates -> ``-1`` (one half size **smaller**; Requirement 1.5).
    * ``too_small`` dominates -> ``+1`` (one half size **larger**; Requirement 1.6).
    * neither dominates -> ``0`` (keep the Stated_Usual_Size; Requirement 1.7).

    The gap is measured as the difference in the two leans' shares of the combined
    (too_big + too_small) fit-signal total. When there are no directional fit
    signals, the direction is neutral.

    Args:
        too_big_count: Count of "Too big" Fit_Return_Signals for the SKU.
        too_small_count: Count of "Too small" Fit_Return_Signals for the SKU.

    Returns:
        ``-1``, ``0``, or ``+1`` — a half-size step direction.

    Raises:
        ValueError: If either count is negative.
    """
    if too_big_count < 0 or too_small_count < 0:
        raise ValueError(
            f"counts must be non-negative, got too_big={too_big_count!r}, "
            f"too_small={too_small_count!r}"
        )

    directional_total = too_big_count + too_small_count
    if directional_total == 0:
        return 0

    too_big_pct = 100.0 * too_big_count / directional_total
    too_small_pct = 100.0 * too_small_count / directional_total

    if too_big_pct - too_small_pct >= DOMINANCE_GAP_PP:
        return -1  # runs big -> size down
    if too_small_pct - too_big_pct >= DOMINANCE_GAP_PP:
        return +1  # runs small -> size up
    return 0


# ---------------------------------------------------------------------------
# Confidence tiers (ordinal low=0, medium=1, high=2)
# ---------------------------------------------------------------------------

LOW: str = "low"
MEDIUM: str = "medium"
HIGH: str = "high"

# Ordinal scale: adjustments move at most one step and the result is clamped to
# [0, 2] (design "Confidence arithmetic").
TIER_INDEX: dict[str, int] = {LOW: 0, MEDIUM: 1, HIGH: 2}
INDEX_TIER: dict[int, str] = {index: tier for tier, index in TIER_INDEX.items()}

# Base-tier thresholds on the OMS Fit_Return_Signals + Exchange_Signals count
# (Requirements 1.3, 1.4): < 5 -> low, 5-14 -> medium, >= 15 -> high.
MEDIUM_SIGNAL_THRESHOLD: int = 5
HIGH_SIGNAL_THRESHOLD: int = 15


def base_confidence_tier(signal_count: int) -> str:
    """Return the base Confidence_Level tier from the OMS signal volume.

    * ``signal_count < 5`` -> :data:`LOW` (below Minimum_Signal_Threshold;
      Requirement 1.3).
    * ``5 <= signal_count < 15`` -> :data:`MEDIUM` (Requirement 1.4).
    * ``signal_count >= 15`` -> :data:`HIGH` (Requirement 1.4).

    Here ``signal_count`` is the combined Fit_Return_Signals + Exchange_Signals
    (and outcome signals) count for the SKU.

    Args:
        signal_count: The supporting-signal count driving the base tier.

    Returns:
        One of :data:`LOW`, :data:`MEDIUM`, :data:`HIGH`.

    Raises:
        ValueError: If ``signal_count`` is negative.
    """
    if signal_count < 0:
        raise ValueError(f"signal_count must be non-negative, got {signal_count!r}")
    if signal_count >= HIGH_SIGNAL_THRESHOLD:
        return HIGH
    if signal_count >= MEDIUM_SIGNAL_THRESHOLD:
        return MEDIUM
    return LOW


def clamp_tier(tier: str) -> str:
    """Clamp an arbitrary tier label into the recognized ``{low, medium, high}`` set.

    An already-valid label is returned unchanged. The ordinal index is clamped to
    ``[0, 2]`` for any out-of-range integer-like input routed through
    :func:`tier_from_index`.

    Args:
        tier: A tier label to validate.

    Returns:
        One of :data:`LOW`, :data:`MEDIUM`, :data:`HIGH`.

    Raises:
        ValueError: If ``tier`` is not a recognized tier label.
    """
    if tier not in TIER_INDEX:
        raise ValueError(f"unrecognized confidence tier: {tier!r}")
    return tier


def tier_from_index(index: int) -> str:
    """Return the tier label for an ordinal index, clamped to ``[0, 2]``.

    Used after tier arithmetic so any adjustment that would push below ``low`` or
    above ``high`` is clamped to the nearest valid tier (design "Tier arithmetic").

    Args:
        index: An ordinal tier index (may be out of range).

    Returns:
        One of :data:`LOW`, :data:`MEDIUM`, :data:`HIGH`.
    """
    clamped = max(0, min(2, index))
    return INDEX_TIER[clamped]


def adjust_tier(tier: str, delta: int) -> str:
    """Move ``tier`` by ``delta`` ordinal steps, clamped to ``[low, high]``.

    Each confidence adjustment in the design moves at most one step; this helper
    applies a single ordered adjustment and clamps the result (Requirements 1.17,
    1.18, 1.20). Adjustments are applied in a fixed order by the caller so the
    outcome is deterministic.

    Args:
        tier: The current tier label.
        delta: Signed number of ordinal steps to move (typically ``-1``, ``0``,
            or ``+1``).

    Returns:
        The adjusted tier, clamped into ``{low, medium, high}``.

    Raises:
        ValueError: If ``tier`` is not a recognized tier label.
    """
    return tier_from_index(TIER_INDEX[clamp_tier(tier)] + delta)
