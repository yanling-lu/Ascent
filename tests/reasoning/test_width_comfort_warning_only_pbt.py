"""Property-based test for the Guardrail Validator's width/comfort warning-only rule.

Feature: ai-fit, Property 7: Width, shape, and comfort signals never change the size.

Design "Part B" Invariant 4 (and the design's width/comfort warning-only section)
states that Review_Width_Signals, Customer_Fit_Profile width/shape flags, the
Catalog_Toe_Box, and the proposed width_warning/comfort_note hints are **advisory
only**: the validator recomputes warnings from the signals and never lets any of
them move the Recommended_Size (or the Confidence_Level).

This property fixes a base ``Signals`` + ``ProposedDecision`` and then builds two
variants that differ *only* in those width/shape/comfort fields:

* ``WidthSignals`` — review width majority/count, the four customer width flags
  (too narrow / too wide / tight toes / heels slip), and the catalog ``toe_box``;
* the proposed ``width_warning`` and ``comfort_note`` hints.

Everything else (request type, invoked tools, stated size, sizing system, offered
sizes, OMS counts, foot measurement, proposed size and confidence) is held
identical across the two variants. The validated ``size`` and ``confidence`` must
therefore be equal — width/comfort can only change the attached ``warnings``.

Validates: Requirements 3.9, 3.11, 3.12, 3.13
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from hypothesis import given, settings
from hypothesis import strategies as st

from ai_fit.reasoning.sizing import HALF_SIZE, apply_half_steps
from ai_fit.reasoning.validator import (
    REQUEST_SIZE,
    CatalogView,
    OmsView,
    ProposedDecision,
    Signals,
    ValidatedDecision,
    WidthSignals,
    validate,
)

# All mandatory tools for a size recommendation with no customer id supplied, so
# the required-tool guard passes and the validator emits a ValidatedDecision.
_SIZE_TOOLS = frozenset(
    {"get_sku_fit_signals", "get_product_catalog_entry", "get_sku_reviews"}
)

_TIERS = ("low", "medium", "high")

# Values the width/shape fields may take. ``None``/"normal"/"regular" are the
# "no concern" members; the others may raise a warning but must never move the size.
_REVIEW_MAJORITIES = (None, "normal", "narrow", "wide")
_TOE_BOXES = (None, "regular", "narrow", "roomy")


@st.composite
def _width_signals(draw: st.DrawFn) -> WidthSignals:
    """An arbitrary WidthSignals bundle spanning concern and no-concern states."""
    return WidthSignals(
        review_width_count=draw(st.integers(min_value=0, max_value=10)),
        review_width_majority=draw(st.sampled_from(_REVIEW_MAJORITIES)),
        customer_too_narrow=draw(st.booleans()),
        customer_too_wide=draw(st.booleans()),
        customer_tight_toes=draw(st.booleans()),
        customer_heels_slip=draw(st.booleans()),
        toe_box=draw(st.sampled_from(_TOE_BOXES)),
    )


@st.composite
def _optional_hint(draw: st.DrawFn) -> Optional[str]:
    """An optional proposed width_warning / comfort_note hint (advisory only)."""
    return draw(st.one_of(st.none(), st.sampled_from(("", "snug", "roomy", "cushioned"))))


@st.composite
def _base_and_two_width_variants(draw: st.DrawFn):
    """A base size recommendation plus two variants differing only in width/comfort.

    The base holds every size-relevant input fixed: an OMS object is present (so the
    tool-failure short-circuit does not trigger), ``foot_length_cm`` is drawn once
    and shared, the offered grid always contains the proposed size and the stated
    size so the size-affecting invariants resolve identically for both variants. The
    two variants swap in independently drawn ``WidthSignals`` and proposed
    width_warning/comfort_note hints — and nothing else.
    """
    stated = 5.0 + draw(st.integers(min_value=0, max_value=20)) * HALF_SIZE

    # Proposed size within one half step of stated so it is a realistic, un-capped
    # proposal; the exact value does not matter to the property, only that it is the
    # same for both variants.
    proposed_size = apply_half_steps(stated, draw(st.sampled_from((-1, 0, 1))))

    # Offered grid includes stated, proposed, and their neighbours so the
    # offered-range clamp lands deterministically on the same size for both variants.
    offered = sorted(
        {
            round(s, 1)
            for s in (
                stated - HALF_SIZE,
                stated,
                stated + HALF_SIZE,
                proposed_size - HALF_SIZE,
                proposed_size,
                proposed_size + HALF_SIZE,
            )
        }
    )

    # Optionally present a Foot_Measurement; shared by both variants either way so
    # measurement precedence (if it fires) resolves to the same size for both.
    foot_length_cm = draw(st.one_of(st.none(), st.floats(min_value=20.0, max_value=30.0)))

    oms = OmsView(
        too_big_count=draw(st.integers(min_value=0, max_value=40)),
        too_small_count=draw(st.integers(min_value=0, max_value=40)),
        uncomfortable_count=draw(st.integers(min_value=0, max_value=20)),
        exchange_count=draw(st.integers(min_value=0, max_value=20)),
    )

    # The catalog toe_box lives on CatalogView too, but the width/comfort invariant
    # concerns WidthSignals.toe_box; hold the CatalogView toe_box fixed and vary only
    # the width bundle so the only moving parts are the warning-only fields.
    catalog = CatalogView(offered_sizes=tuple(offered))
    confidence = draw(st.sampled_from(_TIERS))

    def make_signals(width: WidthSignals) -> Signals:
        return Signals(
            request_type=REQUEST_SIZE,
            invoked_tools=_SIZE_TOOLS,
            stated_usual_size=stated,
            sizing_system="Women's sizing",
            catalog=catalog,
            oms=oms,
            foot_length_cm=foot_length_cm,
            customer_id_supplied=False,
            width=width,
        )

    def make_proposed(width_warning: Optional[str], comfort_note: Optional[str]) -> ProposedDecision:
        return ProposedDecision(
            recommended_size=proposed_size,
            confidence_level=confidence,
            size_rationale="based on fit-return history",
            cited_signals=("oms_fit_returns",),
            width_warning=width_warning,
            comfort_note=comfort_note,
        )

    width_a = draw(_width_signals())
    width_b = draw(_width_signals())
    signals_a = make_signals(width_a)
    signals_b = make_signals(width_b)
    proposed_a = make_proposed(draw(_optional_hint()), draw(_optional_hint()))
    proposed_b = make_proposed(draw(_optional_hint()), draw(_optional_hint()))

    return (proposed_a, signals_a), (proposed_b, signals_b)


@settings(max_examples=300)
@given(_base_and_two_width_variants())
def test_width_comfort_signals_never_change_size(case) -> None:
    """Varying only width/shape/comfort fields leaves size and confidence unchanged.

    Feature: ai-fit, Property 7: Width, shape, and comfort signals never change the
    size.

    Two inputs that differ only in Review_Width_Signals, the customer width/shape
    flags, the catalog toe box, and the proposed width_warning/comfort_note hints
    must yield the same validated Recommended_Size and the same Confidence_Level.
    Width/comfort can only affect the attached warnings.
    """
    (proposed_a, signals_a), (proposed_b, signals_b) = case

    result_a = validate(proposed_a, signals_a)
    result_b = validate(proposed_b, signals_b)

    # The base inputs always satisfy the hard preconditions, so both validate.
    assert isinstance(result_a, ValidatedDecision)
    assert isinstance(result_b, ValidatedDecision)

    # Sanity: the two variants truly differ only in the width/comfort fields.
    assert replace(signals_a, width=signals_b.width) == signals_b
    assert replace(
        proposed_a,
        width_warning=proposed_b.width_warning,
        comfort_note=proposed_b.comfort_note,
    ) == proposed_b

    assert result_a.size == result_b.size, (
        f"width/comfort changed the size: {result_a.size} != {result_b.size} "
        f"(width_a={signals_a.width}, width_b={signals_b.width})"
    )
    assert result_a.confidence == result_b.confidence, (
        f"width/comfort changed the confidence: "
        f"{result_a.confidence} != {result_b.confidence}"
    )
