"""Property-based test: recommendations require their mandatory tool calls.

Feature: ai-fit, Property 17: Recommendations require their mandatory tool calls.

*For any* set of invoked tools, the Guardrail Validator SHALL reject a
recommendation whenever a mandatory tool for its request type was not invoked,
and SHALL NOT reject it for that reason once every mandatory tool was invoked.

**Validates: Requirements 9.7**

The required-tool guard is the first invariant the validator enforces (design
"Part B — The Guardrail Validator", invariant 5; "Orchestrator interface"
required-tool guard). The mandatory sets are:

* ``REQUEST_SIZE``: ``get_sku_fit_signals``, ``get_product_catalog_entry``,
  ``get_sku_reviews`` — plus ``get_customer_profile`` when a customer id was
  supplied.
* ``REQUEST_OCCASION``: ``get_product_catalog_entry``.

This test drives the guard directly by generating arbitrary subsets of the tool
universe and asserting the guard's two-sided behaviour. To isolate the
required-tool reason from the other reject path (protected attributes), every
generated ``Signals`` keeps ``protected_attributes_present`` empty and ``oms``
present, so the ONLY rejection the validator can emit is "required tool not
called". That makes "not rejected for the tool-guard reason" observable as "not a
``RejectedDecision`` with that reason".
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from ai_fit.reasoning.sizing import HIGH, LOW, MEDIUM, MENS_SIZING, WOMENS_SIZING
from ai_fit.reasoning.validator import (
    REQUEST_OCCASION,
    REQUEST_SIZE,
    CatalogView,
    OmsView,
    ProposedDecision,
    RejectedDecision,
    Signals,
    _OCCASION_REQUIRED_TOOLS,
    _SIZE_REQUIRED_TOOLS,
    required_tools_called,
    validate,
)

# Minimum iterations mandated by the design testing strategy.
PBT_SETTINGS = settings(max_examples=300)

REQUIRED_TOOL_REASON = "required tool not called"

# The universe of tool names the generator draws from: every tool that can be
# mandatory for either request type, plus the customer-profile tool and a couple
# of irrelevant reads whose presence/absence must never affect the guard.
_TOOL_UNIVERSE = sorted(
    _SIZE_REQUIRED_TOOLS
    | _OCCASION_REQUIRED_TOOLS
    | {"get_customer_profile", "get_recommendation_outcomes", "find_alternative_styles"}
)

# Half-size grid used for sizes and offered ranges.
_HALF_SIZES = st.sampled_from([round(5.0 + 0.5 * i, 1) for i in range(16)])  # 5.0..12.5
_CONFIDENCE = st.sampled_from([LOW, MEDIUM, HIGH])


def _mandatory(request_type: str, customer_id_supplied: bool) -> frozenset[str]:
    """The mandatory tool set the guard enforces, mirroring the design spec."""
    if request_type == REQUEST_SIZE:
        required = set(_SIZE_REQUIRED_TOOLS)
        if customer_id_supplied:
            required.add("get_customer_profile")
        return frozenset(required)
    if request_type == REQUEST_OCCASION:
        return frozenset(_OCCASION_REQUIRED_TOOLS)
    return frozenset()


@st.composite
def invoked_subsets(draw):
    """An arbitrary subset of the tool universe (any combination, incl. empty)."""
    chosen = draw(
        st.lists(st.sampled_from(_TOOL_UNIVERSE), max_size=len(_TOOL_UNIVERSE), unique=True)
    )
    return frozenset(chosen)


@st.composite
def guard_signals(draw):
    """Signals whose only reachable rejection is the required-tool guard.

    ``request_type``, ``invoked_tools``, and ``customer_id_supplied`` are the
    guard's inputs and are generated freely. Everything else is a legitimate,
    well-formed fit input: ``oms`` is always present (no tool-failure
    short-circuit) and ``protected_attributes_present`` stays empty (no
    protected-attribute rejection), so the required-tool guard is the sole gate.
    """
    request_type = draw(st.sampled_from([REQUEST_SIZE, REQUEST_OCCASION]))
    customer_id_supplied = draw(st.booleans())
    invoked = draw(invoked_subsets())
    return Signals(
        request_type=request_type,
        invoked_tools=invoked,
        stated_usual_size=draw(_HALF_SIZES),
        sizing_system=draw(st.sampled_from([WOMENS_SIZING, MENS_SIZING])),
        catalog=CatalogView(offered_sizes=(6.0, 6.5, 7.0, 7.5, 8.0)),
        oms=OmsView(
            too_big_count=draw(st.integers(min_value=0, max_value=20)),
            too_small_count=draw(st.integers(min_value=0, max_value=20)),
            uncomfortable_count=draw(st.integers(min_value=0, max_value=20)),
            exchange_count=draw(st.integers(min_value=0, max_value=20)),
        ),
        foot_length_cm=None,
        customer_id_supplied=customer_id_supplied,
        outcome_signal_count=draw(st.integers(min_value=0, max_value=10)),
        protected_attributes_present=frozenset(),
    )


@st.composite
def proposed_decisions(draw):
    """An arbitrary proposed decision; its contents must not affect the guard."""
    return ProposedDecision(
        recommended_size=draw(_HALF_SIZES),
        confidence_level=draw(_CONFIDENCE),
        size_rationale=draw(st.text(max_size=40)),
        cited_signals=tuple(draw(st.lists(st.text(max_size=12), max_size=4))),
    )


@given(proposed=proposed_decisions(), signals=guard_signals())
@PBT_SETTINGS
def test_required_tool_guard_two_sided(proposed, signals):
    """Property 17: the guard rejects iff a mandatory tool was not invoked.

    Two-sided over an arbitrary invoked-tool subset:

    * mandatory set NOT a subset of invoked  => ``RejectedDecision`` whose reason
      is exactly ``"required tool not called"``.
    * mandatory set IS a subset of invoked   => NOT rejected for the tool-guard
      reason (with protected attrs absent and OMS present, it is not a
      ``RejectedDecision`` at all).
    """
    required = _mandatory(signals.request_type, signals.customer_id_supplied)
    all_called = required.issubset(signals.invoked_tools)

    # The helper and the validator agree on whether the mandatory tools were met.
    assert (
        required_tools_called(
            signals.invoked_tools, signals.request_type, signals.customer_id_supplied
        )
        == all_called
    )

    result = validate(proposed, signals)

    if not all_called:
        # A mandatory tool is missing => reject for the required-tool reason.
        assert isinstance(result, RejectedDecision), (
            "missing a mandatory tool must reject the recommendation"
        )
        assert result.reason == REQUIRED_TOOL_REASON
    else:
        # Every mandatory tool was invoked => never rejected for the guard reason.
        # Because protected attrs are absent and OMS is present, the only possible
        # rejection path is closed, so the result is not a RejectedDecision.
        assert not isinstance(result, RejectedDecision), (
            "a recommendation with all mandatory tools invoked must not be rejected"
        )


@given(
    proposed=proposed_decisions(),
    request_type=st.sampled_from([REQUEST_SIZE, REQUEST_OCCASION]),
    customer_id_supplied=st.booleans(),
)
@PBT_SETTINGS
def test_dropping_any_single_mandatory_tool_rejects(
    proposed, request_type, customer_id_supplied
):
    """Each mandatory tool is individually necessary.

    Starting from the full mandatory set (which must be accepted by the guard),
    removing ANY one mandatory tool flips the guard to reject. This pins that the
    guard requires the whole set, not merely a non-empty overlap.
    """
    required = _mandatory(request_type, customer_id_supplied)

    full = Signals(
        request_type=request_type,
        invoked_tools=required,
        stated_usual_size=6.5,
        sizing_system=WOMENS_SIZING,
        catalog=CatalogView(offered_sizes=(6.0, 6.5, 7.0, 7.5, 8.0)),
        oms=OmsView(too_big_count=1, too_small_count=1),
        foot_length_cm=None,
        customer_id_supplied=customer_id_supplied,
        protected_attributes_present=frozenset(),
    )
    assert not isinstance(validate(proposed, full), RejectedDecision)

    for missing in required:
        reduced = Signals(
            request_type=request_type,
            invoked_tools=required - {missing},
            stated_usual_size=6.5,
            sizing_system=WOMENS_SIZING,
            catalog=CatalogView(offered_sizes=(6.0, 6.5, 7.0, 7.5, 8.0)),
            oms=OmsView(too_big_count=1, too_small_count=1),
            foot_length_cm=None,
            customer_id_supplied=customer_id_supplied,
            protected_attributes_present=frozenset(),
        )
        result = validate(proposed, reduced)
        assert isinstance(result, RejectedDecision)
        assert result.reason == REQUIRED_TOOL_REASON
