"""Property-based test for measurement precedence in the Guardrail Validator.

Feature: ai-fit, Property 5: Measurement precedence overrides all other size signals

Design "Part B — The Guardrail Validator", Invariant 2 (measurement precedence):
when a Foot_Measurement is present, the validator overwrites the size with
``measurement_lookup(foot_length_cm, sizing_system)`` regardless of the proposed
size and of every other signal, and the result is then snapped to the SKU's
offered sizes (Invariant 6). This test asserts the end-to-end guarantee: for any
proposed decision and signal bundle in which a foot measurement is present, the
validated Recommended_Size equals
``clamp_to_offered(measurement_lookup(foot_length_cm, sizing_system), offered_sizes)``
— independent of the proposed size and of the OMS / catalog / review / tendency
signals in the bundle.

**Validates: Requirements 1.11, 1.15**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from ai_fit.reasoning.sizing import (
    HIGH,
    LOW,
    MEDIUM,
    MENS_SIZING,
    WOMENS_SIZING,
    clamp_to_offered,
    measurement_lookup,
)
from ai_fit.reasoning.validator import (
    _SIZE_REQUIRED_TOOLS,
    REQUEST_SIZE,
    CatalogView,
    OmsView,
    ProposedDecision,
    Signals,
    ValidatedDecision,
    validate,
)

# A positive, realistic human foot-length range (cm). Spans well below and above
# the measurement table's 22.0 cm anchor so the lookup floor and the men's offset
# are both exercised.
_FOOT_LENGTH_CM = st.floats(
    min_value=18.0, max_value=32.0, allow_nan=False, allow_infinity=False
)

# Both recognized sizing systems (Sizing_System is a legitimate input, Req 1.14).
_SIZING_SYSTEM = st.sampled_from([WOMENS_SIZING, MENS_SIZING])

# Arbitrary proposed size on the half-size grid — the measurement must override it.
_PROPOSED_SIZE = st.floats(
    min_value=0.0, max_value=20.0, allow_nan=False, allow_infinity=False
)

_CONFIDENCE = st.sampled_from([LOW, MEDIUM, HIGH])

# Non-empty offered-size grids on the half-size grid (Req 1.8 clamp target).
_OFFERED_SIZES = st.lists(
    st.sampled_from([3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0, 6.5, 7.0, 7.5,
                     8.0, 8.5, 9.0, 9.5, 10.0, 10.5, 11.0, 11.5, 12.0]),
    min_size=1,
    max_size=10,
    unique=True,
)

# Arbitrary OMS fit/exchange counts — present (not None) so the validator reaches
# the measurement path rather than the tool-failure short-circuit (Req 1.2).
_COUNT = st.integers(min_value=0, max_value=50)


@settings(max_examples=200)
@given(
    foot_length_cm=_FOOT_LENGTH_CM,
    sizing_system=_SIZING_SYSTEM,
    proposed_size=_PROPOSED_SIZE,
    confidence=_CONFIDENCE,
    stated_usual_size=st.sampled_from([5.0, 6.0, 7.0, 8.0, 9.0, 10.0]),
    offered_sizes=_OFFERED_SIZES,
    too_big=_COUNT,
    too_small=_COUNT,
    uncomfortable=_COUNT,
    exchanges=_COUNT,
    fit_guidance=st.sampled_from([None, "runs small", "runs large", "true to size"]),
)
def test_measurement_overrides_all_other_size_signals(
    foot_length_cm,
    sizing_system,
    proposed_size,
    confidence,
    stated_usual_size,
    offered_sizes,
    too_big,
    too_small,
    uncomfortable,
    exchanges,
    fit_guidance,
):
    """Validated size == clamp_to_offered(measurement_lookup(...)), regardless of
    the proposed size or any OMS / catalog / tendency signal."""
    signals = Signals(
        request_type=REQUEST_SIZE,
        invoked_tools=frozenset(_SIZE_REQUIRED_TOOLS),
        stated_usual_size=stated_usual_size,
        sizing_system=sizing_system,
        catalog=CatalogView(
            offered_sizes=tuple(offered_sizes),
            fit_guidance=fit_guidance,
        ),
        oms=OmsView(
            too_big_count=too_big,
            too_small_count=too_small,
            uncomfortable_count=uncomfortable,
            exchange_count=exchanges,
        ),
        foot_length_cm=foot_length_cm,
    )
    proposed = ProposedDecision(
        recommended_size=proposed_size,
        confidence_level=confidence,
        size_rationale="arbitrary proposal",
    )

    result = validate(proposed, signals)

    # The measurement path is reached (not a rejection / short-circuit).
    assert isinstance(result, ValidatedDecision)

    expected = clamp_to_offered(
        measurement_lookup(foot_length_cm, sizing_system),
        list(offered_sizes),
    )
    assert result.size == expected
