"""Property-based test for the Guardrail Validator's non-measurement half-size cap.

Feature: ai-fit, Property 3: Non-measurement size change is capped at one half size

For any proposed decision and signal bundle with NO Foot_Measurement present, the
net size change the validator applies from the Stated_Usual_Size is bounded to one
half size (one step) in net terms. This is the design "Part B" Invariant 1 — the
net single half-size cap (clamp of ``half_steps_between(size, stated)`` to
``[-1, +1]``) — asserted *independently of* the offered-range clamp direction.

To isolate the cap from the offered-range clamp (Invariant 6, which can itself move
the size toward the nearest offered half size), the generated ``offered_sizes``
always include the three capped targets ``{stated - 0.5, stated, stated + 0.5}``.
With those present on the offered grid, the subsequent offered-range snap maps any
capped size to itself, so the final validated size reflects the cap alone. The
proposed sizes are generated far from the stated size (many half steps away, in
both directions) to exercise the cap rather than a within-range proposal.

Validates: Requirements 1.5, 1.6, 1.12, 1.13, 1.19
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from ai_fit.reasoning.sizing import HALF_SIZE, apply_half_steps, half_steps_between
from ai_fit.reasoning.validator import (
    REQUEST_SIZE,
    CatalogView,
    OmsView,
    ProposedDecision,
    Signals,
    ValidatedDecision,
    validate,
)

# All mandatory tools for a size recommendation with no customer id supplied.
_SIZE_TOOLS = frozenset(
    {"get_sku_fit_signals", "get_product_catalog_entry", "get_sku_reviews"}
)

# Confidence tiers the proposed decision may carry (validator clamps/caps them;
# irrelevant to the size cap but kept varied so the proposal is realistic).
_TIERS = ("low", "medium", "high")


@st.composite
def _stated_sizes(draw: st.DrawFn) -> float:
    """A Stated_Usual_Size on the half-size grid in a realistic women's US range."""
    half_steps_from_5 = draw(st.integers(min_value=0, max_value=20))  # 5.0 .. 15.0
    return 5.0 + half_steps_from_5 * HALF_SIZE


@st.composite
def _far_proposed_size(draw: st.DrawFn, stated: float) -> float:
    """A proposed size many half steps away from ``stated`` (both directions).

    Magnitude is at least 2 half steps so the cap (|net| <= 1) always has to fire;
    the sign is drawn freely so both the size-up and size-down caps are exercised.
    """
    magnitude = draw(st.integers(min_value=2, max_value=12))
    direction = draw(st.sampled_from((-1, 1)))
    return apply_half_steps(stated, direction * magnitude)


@st.composite
def _no_measurement_size_signals(draw: st.DrawFn):
    """Signals + proposed decision for a non-measurement size recommendation.

    ``foot_length_cm`` is always ``None`` (no Foot_Measurement -> the cap path, not
    measurement precedence). ``offered_sizes`` always include the three capped
    targets so the offered-range clamp cannot be what bounds the size. OMS is
    present (so the tool-failure short-circuit does not trigger) with arbitrary
    non-negative counts.
    """
    stated = draw(_stated_sizes())
    proposed_size = draw(_far_proposed_size(stated))

    # Offered grid: always include the capped targets, plus arbitrary extra sizes
    # (which must NOT sit between stated and a capped target, else they'd be the
    # nearest snap; extras are placed strictly outside [stated-0.5, stated+0.5]).
    capped_targets = [stated - HALF_SIZE, stated, stated + HALF_SIZE]
    extra_steps = draw(
        st.lists(st.integers(min_value=2, max_value=12), min_size=0, max_size=4)
    )
    extras = [apply_half_steps(stated, s) for s in extra_steps]
    extras += [apply_half_steps(stated, -s) for s in extra_steps]
    offered = sorted({round(s, 1) for s in capped_targets + extras})

    oms = OmsView(
        too_big_count=draw(st.integers(min_value=0, max_value=40)),
        too_small_count=draw(st.integers(min_value=0, max_value=40)),
        uncomfortable_count=draw(st.integers(min_value=0, max_value=20)),
        exchange_count=draw(st.integers(min_value=0, max_value=20)),
    )

    signals = Signals(
        request_type=REQUEST_SIZE,
        invoked_tools=_SIZE_TOOLS,
        stated_usual_size=stated,
        sizing_system="Women's sizing",
        catalog=CatalogView(offered_sizes=tuple(offered)),
        oms=oms,
        foot_length_cm=None,  # no Foot_Measurement -> cap path
        customer_id_supplied=False,
    )
    proposed = ProposedDecision(
        recommended_size=proposed_size,
        confidence_level=draw(st.sampled_from(_TIERS)),
    )
    return signals, proposed


@settings(max_examples=200)
@given(_no_measurement_size_signals())
def test_non_measurement_shift_capped_to_one_half_size(case) -> None:
    """The net non-measurement shift from the stated size is clamped to +-1 step.

    Feature: ai-fit, Property 3: Non-measurement size change is capped at one half
    size.

    With the offered grid containing the capped targets, the offered-range clamp is
    a no-op on a capped size, so the final validated size is at most one half step
    from the Stated_Usual_Size no matter how far the proposed size reached.
    """
    signals, proposed = case
    result = validate(proposed, signals)

    # No measurement, OMS present, required tools called -> a compliant decision.
    assert isinstance(result, ValidatedDecision)

    net_steps = half_steps_between(result.size, signals.stated_usual_size)
    assert abs(net_steps) <= 1, (
        f"net shift {net_steps} half steps exceeds the cap of 1 "
        f"(stated={signals.stated_usual_size}, proposed={proposed.recommended_size}, "
        f"validated={result.size}, offered={list(signals.catalog.offered_sizes)})"
    )

    # Equivalent magnitude check in size units: at most one HALF_SIZE away.
    assert abs(result.size - signals.stated_usual_size) <= HALF_SIZE + 1e-9
