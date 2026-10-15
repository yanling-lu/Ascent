"""Property-based test for outcome-signal reinforcement (Task 5.11).

Feature: ai-fit, Property 15: Outcome signals reinforce and are counted

Outcome_Fit_Signals are a *reinforcing* learning-loop signal: they add to the
supporting-signal total the recommendation reports, but they never override the
size the OMS/measurement/catalog path produces (design "Learning Loop";
``Signals.supporting_signal_count``; ``validate`` in
``ai_fit/reasoning/validator.py``). Two things must therefore hold for every
signal bundle and every proposed decision:

1. **Counted** — the validator's reported ``supporting_signal_count`` equals the
   OMS signal count (or ``0`` when the fit-history tool failed and ``oms is
   None``) plus ``outcome_signal_count``. Outcome signals are added, not folded
   into the OMS threshold.
2. **Reinforce, never override** — two inputs identical except for
   ``outcome_signal_count`` yield the *same* validated size. Varying the outcome
   count changes only the reported support total, never the recommended size.

**Validates: Requirements 4.4, 1.10**

The generators span every validator branch (both request types, required-tool
subsets, OMS present/absent, measurement present/absent, width signals, possible
protected-attribute leak) so the "size unchanged" claim is checked across the
whole input space, not just the happy path. When a bundle rejects, it rejects
regardless of the outcome count — the property treats that consistently.
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
# Strategies — the full validator input space (mirrors the determinism PBT)
# ---------------------------------------------------------------------------

_half_sizes = st.sampled_from([float(n) / 2 for n in range(2, 31)])  # 1.0 .. 15.0

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

_tiers = st.sampled_from([LOW, MEDIUM, HIGH])
_sizing_systems = st.sampled_from([WOMENS_SIZING, MENS_SIZING])

# Outcome-signal counts, including 0 so the "no outcomes" baseline is covered.
_outcome_counts = st.integers(min_value=0, max_value=20)


@st.composite
def _offered_sizes(draw) -> tuple[float, ...]:
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
def _base_signals_without_outcomes(draw) -> Signals:
    """A full signal bundle with ``outcome_signal_count`` fixed at 0.

    Everything else that drives the validator's size and confidence is drawn
    freely; the outcome count is the single axis the property varies, so it is
    pinned here and overridden per-variant below.
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
        outcome_signal_count=0,
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
    return ProposedDecision(
        recommended_size=draw(_half_sizes),
        confidence_level=draw(_tiers),
        size_rationale=draw(st.text(max_size=40)),
        cited_signals=tuple(draw(st.lists(st.text(max_size=12), max_size=4))),
        width_warning=draw(st.one_of(st.none(), st.text(max_size=20))),
        comfort_note=draw(st.one_of(st.none(), st.text(max_size=20))),
    )


def _with_outcome_count(base: Signals, count: int) -> Signals:
    """Return a copy of ``base`` differing only in ``outcome_signal_count``.

    Signals is a frozen dataclass, so rebuild it field-for-field; the only field
    that changes between the two variants the property compares is the outcome
    count.
    """
    return Signals(
        request_type=base.request_type,
        invoked_tools=base.invoked_tools,
        stated_usual_size=base.stated_usual_size,
        sizing_system=base.sizing_system,
        catalog=base.catalog,
        oms=base.oms,
        foot_length_cm=base.foot_length_cm,
        customer_id_supplied=base.customer_id_supplied,
        width=base.width,
        outcome_signal_count=count,
        protected_attributes_present=base.protected_attributes_present,
    )


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(
    proposed=_proposed_decisions(),
    base=_base_signals_without_outcomes(),
    outcome_count=_outcome_counts,
)
def test_outcome_signals_reinforce_and_are_counted(proposed, base, outcome_count):
    """Outcome signals add to the support count but never move the size.

    Feature: ai-fit, Property 15: Outcome signals reinforce and are counted
    Validates: Requirements 4.4, 1.10
    """
    # Two inputs identical except for the number of Outcome_Fit_Signals.
    without = _with_outcome_count(base, 0)
    with_outcomes = _with_outcome_count(base, outcome_count)

    result_without = validate(proposed, without)
    result_with = validate(proposed, with_outcomes)

    # Varying the outcome count must not flip the reject/accept decision: the
    # reject guards (required-tool, protected-attribute) do not read it.
    assert isinstance(result_without, RejectedDecision) == isinstance(
        result_with, RejectedDecision
    )

    if isinstance(result_with, RejectedDecision):
        assert isinstance(result_without, RejectedDecision)
        assert result_with.reason == result_without.reason
        return

    assert isinstance(result_without, ValidatedDecision)
    assert isinstance(result_with, ValidatedDecision)

    # (1) Counted: supporting_signal_count == OMS signal count (0 if oms is None)
    #     + outcome_signal_count.
    oms_count = base.oms.signal_count if base.oms is not None else 0
    assert result_without.supporting_signal_count == oms_count + 0
    assert result_with.supporting_signal_count == oms_count + outcome_count

    # The reported count rises by exactly the number of outcome signals added.
    assert (
        result_with.supporting_signal_count
        - result_without.supporting_signal_count
        == outcome_count
    )

    # (2) Reinforce, never override: the validated size is unchanged by the
    #     presence or quantity of outcome signals. Confidence is likewise
    #     untouched, since outcome signals do not gate the OMS threshold.
    assert result_with.size == result_without.size
    assert result_with.confidence == result_without.confidence
