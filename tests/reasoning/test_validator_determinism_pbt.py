"""Property-based test for Guardrail Validator determinism (Task 5.2).

Feature: ai-fit, Property 1: Determinism of the validator for identical inputs

For any signal bundle and any fixed proposed decision, invoking the Guardrail
Validator twice SHALL produce an identical result — same Recommended_Size and
Confidence_Level (plus warnings, corrections, supporting-signal count, note), or
an identical RejectedDecision reason. The validator is a pure, deterministic
function (no I/O, no randomness, no mutable module state), which is what lets the
system satisfy Requirement 10.4 (identical signals -> identical recommendation).

**Validates: Requirements 10.4**

The design notes that the sizing properties hold for the validator's *final
emitted recommendation regardless of the proposed decision Claude supplies*, so
the generators here range over arbitrary (including deliberately invalid or
out-of-direction) proposed decisions and signal bundles. The property only
asserts that two calls on the *same* inputs agree; it makes no claim about what
that agreed-upon output is.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from ai_fit.reasoning.sizing import HIGH, LOW, MEDIUM, MENS_SIZING, WOMENS_SIZING
from ai_fit.reasoning.validator import (
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
    validate,
)

# ---------------------------------------------------------------------------
# Strategies — generate the full validator input space (including invalid cases)
# ---------------------------------------------------------------------------

# Half-size grid values Claude might propose or a customer might state. Kept to a
# plausible shoe-size span but intentionally covers values the SKU may not offer,
# so the offered-range clamp and the half-size cap are both exercised.
_half_sizes = st.sampled_from([float(n) / 2 for n in range(2, 31)])  # 1.0 .. 15.0

# A tool name universe spanning the mandatory tools (so the required-tool guard
# sometimes passes and sometimes rejects) plus some irrelevant extras.
_tool_names = st.sampled_from(
    [
        "get_sku_fit_signals",
        "get_product_catalog_entry",
        "get_sku_reviews",
        "get_customer_profile",
        "get_recommendation_outcomes",
        "get_quality_returns_and_units",
    ]
)

# Confidence labels: only the three valid tiers, because an unrecognized label
# makes clamp_tier raise (that is a parser contract upstream, not a validator
# determinism concern). Determinism of the valid space is what Property 1 asserts.
_tiers = st.sampled_from([LOW, MEDIUM, HIGH])

_sizing_systems = st.sampled_from([WOMENS_SIZING, MENS_SIZING])


@st.composite
def _offered_sizes(draw) -> tuple[float, ...]:
    """A non-empty, de-duplicated tuple of offered half sizes for a SKU."""
    sizes = draw(st.lists(_half_sizes, min_size=1, max_size=8, unique=True))
    return tuple(sorted(sizes))


@st.composite
def _catalog_views(draw) -> CatalogView:
    return CatalogView(
        offered_sizes=draw(_offered_sizes()),
        fit_guidance=draw(
            st.sampled_from([None, "runs small", "runs large", "true to size"])
        ),
        fit_return_rate=draw(
            st.one_of(st.none(), st.floats(min_value=0.0, max_value=1.0))
        ),
        toe_box=draw(st.sampled_from([None, "regular", "narrow", "roomy"])),
    )


@st.composite
def _oms_views(draw) -> OmsView:
    return OmsView(
        too_big_count=draw(st.integers(min_value=0, max_value=40)),
        too_small_count=draw(st.integers(min_value=0, max_value=40)),
        uncomfortable_count=draw(st.integers(min_value=0, max_value=20)),
        exchange_count=draw(st.integers(min_value=0, max_value=20)),
    )


@st.composite
def _width_signals(draw) -> WidthSignals:
    return WidthSignals(
        review_width_count=draw(st.integers(min_value=0, max_value=10)),
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
def _signals(draw) -> Signals:
    """A full signal bundle spanning every branch the validator can take.

    Covers: both request types; required-tool-present and -missing subsets; OMS
    present (sparse and dense) and absent (tool-failure short-circuit); a
    foot measurement present (measurement precedence) and absent; and a possibly
    leaked protected attribute (reject path).
    """
    return Signals(
        request_type=draw(st.sampled_from([REQUEST_SIZE, REQUEST_OCCASION])),
        invoked_tools=frozenset(
            draw(st.lists(_tool_names, min_size=0, max_size=6, unique=True))
        ),
        stated_usual_size=draw(_half_sizes),
        sizing_system=draw(_sizing_systems),
        catalog=draw(_catalog_views()),
        oms=draw(st.one_of(st.none(), _oms_views())),
        foot_length_cm=draw(
            st.one_of(st.none(), st.floats(min_value=18.0, max_value=32.0))
        ),
        customer_id_supplied=draw(st.booleans()),
        width=draw(_width_signals()),
        outcome_signal_count=draw(st.integers(min_value=0, max_value=20)),
        protected_attributes_present=frozenset(
            draw(
                st.lists(
                    st.sampled_from(sorted(PROTECTED_ATTRIBUTES) + ["sizing_system"]),
                    min_size=0,
                    max_size=3,
                    unique=True,
                )
            )
        ),
    )


@st.composite
def _proposed_decisions(draw) -> ProposedDecision:
    """An arbitrary proposed decision, including sizes the SKU does not offer."""
    return ProposedDecision(
        recommended_size=draw(_half_sizes),
        confidence_level=draw(_tiers),
        size_rationale=draw(st.text(max_size=40)),
        cited_signals=tuple(draw(st.lists(st.text(max_size=12), max_size=4))),
        width_warning=draw(st.one_of(st.none(), st.text(max_size=20))),
        comfort_note=draw(st.one_of(st.none(), st.text(max_size=20))),
    )


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


def _result_key(result):
    """Project a validation result to a comparable, order-sensitive snapshot.

    Covers everything a caller observes: the rejection reason for a
    RejectedDecision, or the full validated payload (size, confidence, warnings,
    corrections, supporting-signal count, note) for a ValidatedDecision.
    """
    if isinstance(result, RejectedDecision):
        return ("rejected", result.reason)
    assert isinstance(result, ValidatedDecision)
    return (
        "validated",
        result.size,
        result.confidence,
        tuple(result.warnings),
        tuple(
            (c.invariant, c.proposed_value, c.corrected_value)
            for c in result.corrections
        ),
        result.supporting_signal_count,
        result.note,
    )


@settings(max_examples=200)
@given(proposed=_proposed_decisions(), signals=_signals())
def test_validator_is_deterministic(proposed, signals):
    """Two validate() calls on identical inputs produce identical results.

    Feature: ai-fit, Property 1: Determinism of the validator for identical inputs
    Validates: Requirements 10.4
    """
    first = validate(proposed, signals)
    second = validate(proposed, signals)
    assert _result_key(first) == _result_key(second)
