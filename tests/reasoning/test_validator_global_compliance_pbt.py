"""Property-based test: the validator never allows a non-compliant recommendation through.

Feature: ai-fit, Property 18: The validator never allows a non-compliant recommendation through.

*For any* arbitrary proposed decision — including deliberately invalid ones
(over-large size shifts, out-of-range or over-cap confidence, sizes off the
offered grid, sizes that ignore a present measurement) — and *any* assembled
signals (both request types, OMS present/absent, measurement present/absent,
protected present/absent, any subset of invoked tools), the validator's final
emitted output SHALL be EITHER a ``RejectedDecision`` OR a ``ValidatedDecision``
that simultaneously satisfies every hard invariant.

**Validates: Requirements 1.5, 1.6, 1.8, 1.11, 1.12, 1.13, 1.19, 1.20, 3.4, 10.1,
10.2, 10.6**

This is the global/aggregate compliance check on the validator (design "Part B —
The Guardrail Validator" invariants table + Property 18). Rather than isolating a
single invariant like the per-invariant properties do, it generates the *whole*
input space the validator can see and asserts, for every result, the appropriate
bundle of hard invariants holds at once:

* The result is one of exactly two shapes: ``RejectedDecision`` or
  ``ValidatedDecision`` — nothing else, and nothing in between.
* A rejection carries exactly one of the two defined reasons (required tool not
  called / protected attribute present) and nothing else (design "Clamp-or-reject
  behavior": those are the only two reject conditions).
* A ``ValidatedDecision`` ALWAYS satisfies, simultaneously:
    - confidence in {low, medium, high} (invariant 3);
    - size snapped to an offered size when the SKU offers any (invariant 6);
    - sparse (OMS below Minimum_Signal_Threshold, or OMS tool failed) ⇒
      confidence <= medium (invariant 3 sparse cap, Req 1.19/3.4);
    - measurement present ⇒ size == clamp_to_offered(measurement_lookup(...))
      regardless of the proposed size (invariant 2, measurement precedence);
    - no measurement ⇒ the net shift from the stated usual size is at most one
      half size, accounting for the offered-range clamp (invariant 1).
* Protected-attribute non-influence (invariant 4 / Req 10.1/10.2/10.6): any
  protected attribute present ⇒ the result is a rejection, never a size/confidence.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from ai_fit.reasoning.sizing import (
    HIGH,
    LOW,
    MEDIUM,
    MENS_SIZING,
    TIER_INDEX,
    WOMENS_SIZING,
    clamp_to_offered,
    half_steps_between,
    measurement_lookup,
)
from ai_fit.reasoning.validator import (
    MINIMUM_SIGNAL_THRESHOLD,
    PROTECTED_ATTRIBUTES,
    REQUEST_OCCASION,
    REQUEST_SIZE,
    CatalogView,
    OmsView,
    ProposedDecision,
    RejectedDecision,
    Signals,
    ValidatedDecision,
    WidthSignals,
    has_protected_attributes,
    required_tools_called,
    validate,
)

# Minimum 100 iterations mandated by the design testing strategy; use more to
# exercise the large combined input space thoroughly.
PBT_SETTINGS = settings(max_examples=400)

# The full universe of tool names the required-tool guard reasons about. Generated
# invoked-tool sets are drawn from this so we hit both "all required present" and
# "missing a required tool" cases for each request type.
_ALL_TOOLS = sorted(
    {
        "get_sku_fit_signals",
        "get_product_catalog_entry",
        "get_sku_reviews",
        "get_customer_profile",
        "get_recommendation_outcomes",
        "get_quality_returns_and_units",
    }
)

# Confidence labels the validator recognizes (clamp_tier rejects anything else, so
# the proposal only ever carries a recognized label — the clamp still fires for
# the sparse cap).
_CONFIDENCE = st.sampled_from([LOW, MEDIUM, HIGH])

# Half-size grid used for stated sizes and offered ranges.
_HALF_SIZES = st.sampled_from([round(5.0 + 0.5 * i, 1) for i in range(16)])  # 5.0..12.5

# A possibly-empty subset of the five canonical protected attributes, so both the
# "clean" and "leaked" paths are generated.
_PROTECTED_SUBSETS = st.lists(
    st.sampled_from(sorted(PROTECTED_ATTRIBUTES)),
    max_size=len(PROTECTED_ATTRIBUTES),
    unique=True,
).map(frozenset)


@st.composite
def offered_sizes(draw):
    """A non-empty tuple of distinct offered half sizes (order irrelevant)."""
    sizes = draw(st.lists(_HALF_SIZES, min_size=1, max_size=10, unique=True))
    return tuple(sizes)


@st.composite
def width_signals(draw):
    """Arbitrary width/shape inputs — warning-only, never move the size."""
    return WidthSignals(
        review_width_count=draw(st.integers(min_value=0, max_value=50)),
        review_width_majority=draw(st.sampled_from([None, "narrow", "wide", "normal"])),
        customer_too_narrow=draw(st.booleans()),
        customer_too_wide=draw(st.booleans()),
        customer_tight_toes=draw(st.booleans()),
        customer_heels_slip=draw(st.booleans()),
        toe_box=draw(st.sampled_from([None, "regular", "narrow", "roomy"])),
    )


@st.composite
def oms_views(draw):
    """An OMS fit/exchange count bundle, or ``None`` (fit-history tool failed).

    ``None`` exercises the tool-failure short-circuit; a present object spans both
    sides of the Minimum_Signal_Threshold so the sparse cap is hit either way.
    """
    if draw(st.booleans()):
        return None
    return OmsView(
        too_big_count=draw(st.integers(min_value=0, max_value=40)),
        too_small_count=draw(st.integers(min_value=0, max_value=40)),
        uncomfortable_count=draw(st.integers(min_value=0, max_value=40)),
        exchange_count=draw(st.integers(min_value=0, max_value=40)),
    )


@st.composite
def catalog_views(draw):
    """A catalog slice with a non-empty offered-size grid for the clamp."""
    return CatalogView(
        offered_sizes=draw(offered_sizes()),
        fit_guidance=draw(
            st.sampled_from([None, "runs small", "runs large", "true to size"])
        ),
        fit_return_rate=draw(st.one_of(st.none(), st.floats(min_value=0.0, max_value=1.0))),
        toe_box=draw(st.sampled_from([None, "regular", "narrow", "roomy"])),
    )


@st.composite
def signals_strategy(draw):
    """Generate the FULL input space the validator can see.

    Covers both request types, OMS present/absent, measurement present/absent,
    protected present/absent, and arbitrary subsets of invoked tools (so the
    required-tool guard fires and passes across examples).
    """
    request_type = draw(st.sampled_from([REQUEST_SIZE, REQUEST_OCCASION]))
    invoked_tools = frozenset(
        draw(st.lists(st.sampled_from(_ALL_TOOLS), unique=True))
    )
    return Signals(
        request_type=request_type,
        invoked_tools=invoked_tools,
        stated_usual_size=draw(_HALF_SIZES),
        sizing_system=draw(st.sampled_from([WOMENS_SIZING, MENS_SIZING])),
        catalog=draw(catalog_views()),
        oms=draw(oms_views()),
        foot_length_cm=draw(
            st.one_of(st.none(), st.floats(min_value=18.0, max_value=32.0))
        ),
        customer_id_supplied=draw(st.booleans()),
        width=draw(width_signals()),
        outcome_signal_count=draw(st.integers(min_value=0, max_value=20)),
        protected_attributes_present=draw(_PROTECTED_SUBSETS),
    )


@st.composite
def proposed_decisions(draw):
    """An arbitrary proposed decision, including deliberately out-of-grid / over-cap values.

    Sizes range well outside the offered grid and far from any plausible usual
    size so the offered-range clamp and the half-size cap are genuinely exercised.
    """
    return ProposedDecision(
        recommended_size=draw(st.floats(min_value=1.0, max_value=20.0)),
        confidence_level=draw(_CONFIDENCE),
        size_rationale=draw(st.text(max_size=40)),
        cited_signals=tuple(draw(st.lists(st.text(max_size=12), max_size=4))),
        width_warning=draw(st.one_of(st.none(), st.text(max_size=20))),
        comfort_note=draw(st.one_of(st.none(), st.text(max_size=20))),
    )


def _is_sparse(signals: Signals) -> bool:
    """Replicate the validator's sparse predicate for the assertion.

    Sparse = OMS Fit_Return_Signals + Exchange_Signals below the
    Minimum_Signal_Threshold. A failed OMS tool (``oms is None``) is the extreme
    sparse case and short-circuits to low, which also satisfies ``<= medium``.
    """
    oms_count = signals.oms.signal_count if signals.oms is not None else 0
    return oms_count < MINIMUM_SIGNAL_THRESHOLD


@given(proposed=proposed_decisions(), signals=signals_strategy())
@PBT_SETTINGS
def test_validator_global_compliance(proposed, signals):
    """Property 18: every result is a rejection or a fully-compliant decision.

    For ANY (proposed, signals) the validator returns one of exactly two shapes,
    and when it returns a ``ValidatedDecision`` every hard invariant holds at once.
    """
    result = validate(proposed, signals)

    # The result is one of exactly two defined shapes — nothing else.
    assert isinstance(result, (ValidatedDecision, RejectedDecision)), (
        f"validator returned an unexpected type: {type(result)!r}"
    )

    # --- Rejection path: only the two defined reasons, and only when warranted. ---
    if isinstance(result, RejectedDecision):
        tools_missing = not required_tools_called(
            signals.invoked_tools, signals.request_type, signals.customer_id_supplied
        )
        protected_present = has_protected_attributes(signals)
        # Never rejects for a reason other than the two defined ones.
        assert result.reason in (
            "required tool not called",
            "protected attribute present",
        ), f"unexpected rejection reason: {result.reason!r}"
        # And it only rejects when one of those two conditions actually holds.
        assert tools_missing or protected_present, (
            "validator rejected without a required-tool miss or a protected attribute"
        )
        # The required-tool guard runs first, so a missing tool rejects even if a
        # protected attribute is also present. If no tool is missing, the reason
        # must be the protected one.
        if not tools_missing:
            assert result.reason == "protected attribute present"
        return

    # --- Accepted path: a ValidatedDecision must satisfy EVERY hard invariant. ---

    # Preconditions for acceptance must have held (invariant 4/5): required tools
    # were called and NO protected attribute leaked. This is the protected-
    # attribute non-influence guarantee — a tainted input can never yield a size.
    assert required_tools_called(
        signals.invoked_tools, signals.request_type, signals.customer_id_supplied
    ), "accepted despite a missing required tool"
    assert not has_protected_attributes(signals), (
        "accepted despite a protected attribute being present"
    )

    # Invariant 3 — confidence bounds: always one of the three recognized tiers.
    assert result.confidence in (LOW, MEDIUM, HIGH), (
        f"confidence out of range: {result.confidence!r}"
    )

    # Invariant 3 — sparse/medium cap: sparse paths (OMS below threshold, incl. a
    # failed OMS tool) never exceed medium confidence (Req 1.19, 3.4).
    if _is_sparse(signals):
        assert TIER_INDEX[result.confidence] <= TIER_INDEX[MEDIUM], (
            f"sparse recommendation exceeded medium confidence: {result.confidence!r}"
        )

    offered = list(signals.catalog.offered_sizes)

    # Invariant 6 — offered-range membership: when the SKU offers sizes, the final
    # size is one of them (the generator always supplies a non-empty grid).
    if offered:
        assert result.size in offered, (
            f"size {result.size!r} is not one of the offered sizes {offered!r}"
        )

    if signals.oms is None:
        # Tool-failure short-circuit (Req 1.2): size is the stated usual size,
        # snapped to the offered range; confidence is low. This is the sparse
        # extreme and is already covered by the cap assertion above.
        expected = signals.stated_usual_size
        if offered:
            expected = clamp_to_offered(expected, offered)
        assert result.size == expected, (
            "tool-failure path did not fall back to the (clamped) stated usual size"
        )
        assert result.confidence == LOW, (
            "tool-failure path must be low confidence"
        )
        return

    if signals.foot_length_cm is not None:
        # Invariant 2 — measurement precedence (Req 1.11, 1.15): the final size is
        # the measurement lookup, snapped to the offered range, regardless of the
        # proposed size.
        looked_up = measurement_lookup(signals.foot_length_cm, signals.sizing_system)
        expected = clamp_to_offered(looked_up, offered) if offered else looked_up
        assert result.size == expected, (
            "measurement present but final size does not match the clamped lookup"
        )
    else:
        # Invariant 1 — non-measurement net single half-size cap (Req 1.5/1.6/1.12/
        # 1.13/1.19): the net shift from the stated usual size is at most one half
        # size, AFTER accounting for the offered-range clamp. The validator caps
        # the shift to one half step and THEN snaps to the offered grid, so we
        # compute the same reference (the capped size, snapped) and require the
        # emitted size to match it exactly.
        proposed_steps = half_steps_between(
            proposed.recommended_size, signals.stated_usual_size
        )
        if abs(proposed_steps) > 1:
            capped_steps = 1 if proposed_steps > 0 else -1
        else:
            capped_steps = proposed_steps
        capped_size = signals.stated_usual_size + capped_steps * 0.5
        expected = clamp_to_offered(capped_size, offered) if offered else capped_size
        assert result.size == expected, (
            f"non-measurement size {result.size!r} does not match the capped-then-"
            f"clamped size {expected!r} (proposed steps={proposed_steps})"
        )
