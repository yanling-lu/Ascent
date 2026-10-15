"""Property-based test for confidence bounds + the sparse confidence cap (Task 5.7).

Feature: ai-fit, Property: Confidence bounds and sparse cap

The design's "Part B — The Guardrail Validator" enforces two confidence
invariants on Claude's proposed decision (design "Confidence arithmetic", step 4,
and the "Confidence bounds + sparse cap" invariant row):

1. **Bounds** — the final Confidence_Level is always one of ``{low, medium, high}``;
   any proposed tier is clamped into that set (Requirements 1.4, 1.17, 1.18).
2. **Sparse cap** — a catalog-/review-driven recommendation, i.e. one whose OMS
   Fit_Return_Signals + Exchange_Signals count is **below** the
   Minimum_Signal_Threshold (5), is capped at ``medium`` and is never ``high``
   (Requirements 1.19, 1.20, 3.4).

**Validates: Requirements 1.4, 1.17, 1.18, 1.19, 1.20, 3.4**

The property holds for the validator's *final emitted* confidence regardless of
what tier Claude proposed, so the generators range over all three valid proposed
tiers and over OMS signal volumes spanning both below and above the threshold.
To keep every example on the confidence-bearing path (rather than the
required-tool reject, protected-attribute reject, or tool-failure short-circuit),
the signal bundle always invokes the mandatory size tools, carries no protected
attribute, supplies no customer id (so the profile tool is not required), and
provides a present OMS view.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from ai_fit.reasoning.sizing import HIGH, LOW, MEDIUM, MENS_SIZING, WOMENS_SIZING
from ai_fit.reasoning.validator import (
    MINIMUM_SIGNAL_THRESHOLD,
    REQUEST_SIZE,
    CatalogView,
    OmsView,
    ProposedDecision,
    Signals,
    ValidatedDecision,
    validate,
)

# The three mandatory tools for a size recommendation when no customer id is
# supplied (design required-tool guard). Including them keeps the validator on the
# confidence-bearing path rather than returning a required-tool rejection.
_SIZE_TOOLS = frozenset(
    {"get_sku_fit_signals", "get_product_catalog_entry", "get_sku_reviews"}
)

# Valid confidence tiers Claude may propose.
_tiers = st.sampled_from([LOW, MEDIUM, HIGH])

# Half-size grid values for the stated usual size / proposed size.
_half_sizes = st.sampled_from([float(n) / 2 for n in range(2, 31)])  # 1.0 .. 15.0

_sizing_systems = st.sampled_from([WOMENS_SIZING, MENS_SIZING])


@st.composite
def _oms_views(draw) -> OmsView:
    """An OMS view whose total signal_count spans below and above the threshold.

    The four component counts are drawn independently with small ranges so the
    combined ``signal_count`` straddles the Minimum_Signal_Threshold (5): some
    draws land in the sparse region (0..4) and some land dense (>= 5), exercising
    both sides of the cap.
    """
    return OmsView(
        too_big_count=draw(st.integers(min_value=0, max_value=10)),
        too_small_count=draw(st.integers(min_value=0, max_value=10)),
        uncomfortable_count=draw(st.integers(min_value=0, max_value=5)),
        exchange_count=draw(st.integers(min_value=0, max_value=5)),
    )


@st.composite
def _catalog_views(draw) -> CatalogView:
    sizes = draw(st.lists(_half_sizes, min_size=1, max_size=8, unique=True))
    return CatalogView(
        offered_sizes=tuple(sorted(sizes)),
        fit_guidance=draw(
            st.sampled_from([None, "runs small", "runs large", "true to size"])
        ),
        fit_return_rate=draw(
            st.one_of(st.none(), st.floats(min_value=0.0, max_value=1.0))
        ),
        toe_box=draw(st.sampled_from([None, "regular", "narrow", "roomy"])),
    )


@st.composite
def _signals(draw) -> Signals:
    """A size-request bundle on the confidence-bearing path with a present OMS view."""
    return Signals(
        request_type=REQUEST_SIZE,
        invoked_tools=_SIZE_TOOLS,
        stated_usual_size=draw(_half_sizes),
        sizing_system=draw(_sizing_systems),
        catalog=draw(_catalog_views()),
        oms=draw(_oms_views()),
        # No foot measurement: keep the size on the non-measurement path. The
        # confidence invariants are independent of this, but omitting it avoids
        # masking anything behind measurement precedence.
        foot_length_cm=None,
        customer_id_supplied=False,
        outcome_signal_count=draw(st.integers(min_value=0, max_value=20)),
        protected_attributes_present=frozenset(),
    )


@st.composite
def _proposed_decisions(draw) -> ProposedDecision:
    """A proposed decision ranging over all three valid confidence tiers."""
    return ProposedDecision(
        recommended_size=draw(_half_sizes),
        confidence_level=draw(_tiers),
        size_rationale=draw(st.text(max_size=40)),
        cited_signals=tuple(draw(st.lists(st.text(max_size=12), max_size=4))),
    )


@settings(max_examples=300)
@given(proposed=_proposed_decisions(), signals=_signals())
def test_confidence_within_bounds_and_sparse_capped(proposed, signals):
    """Final confidence is always a valid tier; sparse paths are never ``high``.

    Feature: ai-fit, Property: Confidence bounds and sparse cap
    Validates: Requirements 1.4, 1.17, 1.18, 1.19, 1.20, 3.4
    """
    result = validate(proposed, signals)

    # On this path the validator always emits a decision (never rejects).
    assert isinstance(result, ValidatedDecision)

    # Bounds: the emitted confidence is one of the three recognized tiers.
    assert result.confidence in {LOW, MEDIUM, HIGH}

    # Sparse cap: when OMS Fit_Return_Signals + Exchange_Signals are below the
    # Minimum_Signal_Threshold, confidence is capped at medium (never high).
    oms_count = signals.oms.signal_count
    if oms_count < MINIMUM_SIGNAL_THRESHOLD:
        assert result.confidence != HIGH
