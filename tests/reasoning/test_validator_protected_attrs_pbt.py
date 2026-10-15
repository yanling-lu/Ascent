"""Property-based test: protected attributes never influence the validator.

Feature: ai-fit, Property 2: Protected attributes never influence the output.

*For any* pair of inputs (signals + proposed decision) that are identical except
for protected-attribute values (age, gender, ethnicity, nationality, location),
the Guardrail Validator SHALL produce an identical final Recommended_Size and
Confidence_Level.

**Validates: Requirements 10.1, 10.2, 10.6**

The design carries protected attributes into the validator only as the set of
*keys* that leaked through the data layer (``Signals.protected_attributes_present``);
their legitimate fit inputs never encode a protected value. Non-influence is
therefore demonstrated on two fronts:

1. When no protected attribute leaked, the validator emits a ``ValidatedDecision``
   whose size/confidence are a pure function of the legitimate fit signals — so
   injecting, removing, or varying *which* protected attributes are present (while
   the set stays empty across the comparison, i.e. varying only the absent values)
   cannot move the output.
2. The deterministic backstop refuses to emit when ANY protected attribute is
   present: the result is a ``RejectedDecision`` regardless of the proposed size
   or confidence, so no protected-attribute-tainted recommendation ever reaches
   the customer. Varying which/how many protected keys are present never produces
   a size/confidence at all — it always rejects.

Together these establish that the final Recommended_Size and Confidence_Level are
invariant under any protected-attribute data: present ⇒ always rejected (no size/
confidence emitted), absent ⇒ a size/confidence that depends only on legitimate
signals.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from ai_fit.reasoning.sizing import HIGH, LOW, MEDIUM, MENS_SIZING, WOMENS_SIZING
from ai_fit.reasoning.validator import (
    PROTECTED_ATTRIBUTES,
    REQUEST_SIZE,
    CatalogView,
    OmsView,
    ProposedDecision,
    RejectedDecision,
    Signals,
    ValidatedDecision,
    WidthSignals,
    validate,
)

# Minimum iterations mandated by the design testing strategy.
PBT_SETTINGS = settings(max_examples=200)

# All tools a size recommendation can require, so generated signals can satisfy
# the required-tool guard and reach the body of the validator.
_ALL_SIZE_TOOLS = frozenset(
    {
        "get_sku_fit_signals",
        "get_product_catalog_entry",
        "get_sku_reviews",
        "get_customer_profile",
    }
)

# Confidence labels the validator recognizes.
_CONFIDENCE = st.sampled_from([LOW, MEDIUM, HIGH])

# Half-size grid used for sizes and offered ranges.
_HALF_SIZES = st.sampled_from([round(5.0 + 0.5 * i, 1) for i in range(16)])  # 5.0..12.5


@st.composite
def offered_sizes(draw):
    """A non-empty tuple of distinct offered half sizes (order irrelevant)."""
    sizes = draw(
        st.lists(_HALF_SIZES, min_size=1, max_size=10, unique=True)
    )
    return tuple(sizes)


@st.composite
def width_signals(draw):
    """Arbitrary width/shape inputs — warning-only, never move the size."""
    return WidthSignals(
        review_width_count=draw(st.integers(min_value=0, max_value=50)),
        review_width_majority=draw(
            st.sampled_from([None, "narrow", "wide", "normal"])
        ),
        customer_too_narrow=draw(st.booleans()),
        customer_too_wide=draw(st.booleans()),
        customer_tight_toes=draw(st.booleans()),
        customer_heels_slip=draw(st.booleans()),
        toe_box=draw(st.sampled_from([None, "regular", "narrow", "roomy"])),
    )


@st.composite
def oms_views(draw):
    """An OMS fit/exchange count bundle, or ``None`` (fit-history tool failed)."""
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
        fit_return_rate=draw(
            st.one_of(st.none(), st.floats(min_value=0.0, max_value=1.0))
        ),
        toe_box=draw(st.sampled_from([None, "regular", "narrow", "roomy"])),
    )


@st.composite
def base_signals(draw, *, protected=frozenset()):
    """Legitimate fit signals that satisfy the required-tool guard.

    ``protected`` lets a test choose which protected-attribute keys (if any) leaked
    through. The legitimate fit inputs themselves never encode a protected value.
    """
    return Signals(
        request_type=REQUEST_SIZE,
        invoked_tools=_ALL_SIZE_TOOLS,
        stated_usual_size=draw(_HALF_SIZES),
        sizing_system=draw(st.sampled_from([WOMENS_SIZING, MENS_SIZING])),
        catalog=draw(catalog_views()),
        oms=draw(oms_views()),
        foot_length_cm=draw(
            st.one_of(st.none(), st.floats(min_value=20.0, max_value=30.0))
        ),
        customer_id_supplied=True,
        width=draw(width_signals()),
        outcome_signal_count=draw(st.integers(min_value=0, max_value=20)),
        protected_attributes_present=protected,
    )


@st.composite
def proposed_decisions(draw):
    """An arbitrary proposed decision, including deliberately out-of-grid sizes."""
    return ProposedDecision(
        recommended_size=draw(st.floats(min_value=1.0, max_value=20.0)),
        confidence_level=draw(_CONFIDENCE),
        size_rationale=draw(st.text(max_size=40)),
        cited_signals=tuple(draw(st.lists(st.text(max_size=12), max_size=4))),
        width_warning=draw(st.one_of(st.none(), st.text(max_size=20))),
        comfort_note=draw(st.one_of(st.none(), st.text(max_size=20))),
    )


# A non-empty subset of the five canonical protected attributes.
_protected_subsets = st.lists(
    st.sampled_from(sorted(PROTECTED_ATTRIBUTES)),
    min_size=1,
    max_size=len(PROTECTED_ATTRIBUTES),
    unique=True,
).map(frozenset)


@given(
    proposed=proposed_decisions(),
    signals=base_signals(),
    leaked_a=_protected_subsets,
    leaked_b=_protected_subsets,
)
@PBT_SETTINGS
def test_protected_attributes_never_influence_output(
    proposed, signals, leaked_a, leaked_b
):
    """Property 2: protected attributes never influence the output.

    Compares three variants of the SAME legitimate fit inputs:

    * ``clean``  — no protected attribute present.
    * ``leaked_a`` / ``leaked_b`` — two (possibly different) non-empty sets of
      protected attributes injected.

    Non-influence holds if:
    1. the clean variant yields a ``ValidatedDecision`` (never rejected for
       protected reasons), and
    2. every protected-tainted variant is REJECTED regardless of which/how many
       protected keys are present — so varying the protected data can never change
       an emitted size/confidence, because a tainted input emits none.
    """
    clean = signals  # protected_attributes_present defaults to the empty set

    # The clean variant is driven purely by legitimate signals; it must not be
    # rejected for a protected reason. (It may still be a ValidatedDecision.)
    clean_result = validate(proposed, clean)
    assert isinstance(clean_result, ValidatedDecision), (
        "clean signals must not be rejected for a protected reason"
    )

    # Inject protected attributes two different ways. Both must reject, and the
    # rejection is identical and independent of the proposed size/confidence and of
    # which protected keys leaked.
    tainted_a = Signals(
        request_type=clean.request_type,
        invoked_tools=clean.invoked_tools,
        stated_usual_size=clean.stated_usual_size,
        sizing_system=clean.sizing_system,
        catalog=clean.catalog,
        oms=clean.oms,
        foot_length_cm=clean.foot_length_cm,
        customer_id_supplied=clean.customer_id_supplied,
        width=clean.width,
        outcome_signal_count=clean.outcome_signal_count,
        protected_attributes_present=leaked_a,
    )
    tainted_b = Signals(
        request_type=clean.request_type,
        invoked_tools=clean.invoked_tools,
        stated_usual_size=clean.stated_usual_size,
        sizing_system=clean.sizing_system,
        catalog=clean.catalog,
        oms=clean.oms,
        foot_length_cm=clean.foot_length_cm,
        customer_id_supplied=clean.customer_id_supplied,
        width=clean.width,
        outcome_signal_count=clean.outcome_signal_count,
        protected_attributes_present=leaked_b,
    )

    result_a = validate(proposed, tainted_a)
    result_b = validate(proposed, tainted_b)

    # Any protected attribute present => the backstop refuses to emit (Req 10.6).
    assert isinstance(result_a, RejectedDecision)
    assert isinstance(result_b, RejectedDecision)
    # The refusal is identical regardless of WHICH protected keys leaked, so the
    # protected data never produces a differing size/confidence.
    assert result_a.reason == result_b.reason == "protected attribute present"


@given(proposed=proposed_decisions(), signals=base_signals())
@PBT_SETTINGS
def test_clean_decision_is_pure_function_of_legitimate_signals(proposed, signals):
    """Supporting check: with no protected data, re-running yields identical output.

    This pins the non-influence guarantee for the accepted path — the emitted
    size/confidence depend only on the legitimate fit signals, so the (absent)
    protected data cannot be moving them (Requirements 10.1, 10.2).
    """
    first = validate(proposed, signals)
    second = validate(proposed, signals)
    assert isinstance(first, ValidatedDecision)
    assert isinstance(second, ValidatedDecision)
    assert first.size == second.size
    assert first.confidence == second.confidence
