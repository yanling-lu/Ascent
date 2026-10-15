"""The deterministic Guardrail Validator (design "Part B — The Guardrail Validator").

Claude Opus reasons over the tool results under the embedded rubric and proposes a
decision; this module is the pure, deterministic backstop that runs on that
proposal and **enforces the hard invariants** before the recommendation reaches
the customer. It assumes nothing about *how* Claude arrived at the decision — it
only checks and corrects the final emitted recommendation.

``validate(proposed_decision, signals) -> ValidatedDecision`` is **pure and
deterministic**: no I/O, no randomness, no mutable module state. For identical
inputs it always returns an identical result, which is what makes Requirement 10.4
(identical signals -> identical recommendation) hold in practice.

Invariants enforced (design "Invariants enforced" table), in the pseudocode's
order:

1. **Required-tool guard** — reject the recommendation if the mandatory tools for
   its request type were not invoked (Requirement 9.7).
2. **Protected-attribute presence** — reject if any Protected_Attribute slipped
   into the signals (Requirements 10.1, 10.2, 10.6).
3. **Tool-failure short-circuit** — if OMS fit history could not be retrieved,
   recommend the Stated_Usual_Size at ``low`` confidence with an explicit note
   (Requirement 1.2).
4. **Measurement precedence** — when a Foot_Measurement is present, overwrite the
   size with the measurement lookup regardless of the proposed size
   (Requirements 1.11, 1.15).
5. **Net single half-size cap** — for non-measurement paths, clamp a size shift
   larger than one half size back to one half size in the proposed direction
   (Requirements 1.5, 1.6, 1.12, 1.13, 1.19).
6. **Offered-range clamp** — snap the size to the nearest offered half size
   (Requirement 1.8).
7. **Confidence bounds + sparse cap** — clamp confidence into
   ``{low, medium, high}`` and cap catalog-/review-driven (OMS-below-threshold)
   recommendations at ``medium`` (Requirements 1.4, 1.17-1.20, 3.4, 1.19).
8. **Width/comfort warning-only** — width/shape/comfort signals are recomputed
   into warnings and never change the size (Requirements 3.9, 3.11, 3.12, 3.13).

Every clamp or reject is recorded as a correction in the returned decision so the
override is transparent in the Reasoning_Trace (design ReasoningTrace
``validator_corrections``). The validator also exposes the supporting-signal count,
**including outcome signals**, that is reported with the recommendation
(Requirements 1.10, 4.4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Union

from ai_fit.reasoning.sizing import (
    HIGH,
    LOW,
    MEDIUM,
    TIER_INDEX,
    apply_half_steps,
    clamp_tier,
    clamp_to_offered,
    half_steps_between,
    measurement_lookup,
)

# ---------------------------------------------------------------------------
# Protected attributes (Requirements 10.1, 10.2, 10.6)
# ---------------------------------------------------------------------------

# Any of these keys appearing in the signals' protected-attribute set means a
# Protected_Attribute slipped through the data layer; the recommendation is
# rejected rather than silently cleaned, because its presence means an upstream
# guarantee was violated (Requirement 10.6 discards; the validator is the
# backstop that refuses to emit when that guarantee is broken).
PROTECTED_ATTRIBUTES: frozenset[str] = frozenset(
    {"age", "gender", "ethnicity", "nationality", "location"}
)

# ---------------------------------------------------------------------------
# Request types + required-tool guard (Requirement 9.7)
# ---------------------------------------------------------------------------

REQUEST_SIZE: str = "size"
REQUEST_OCCASION: str = "occasion"

# A size recommendation requires fit signals, catalog entry, and reviews; the
# customer profile is required only when a customer id was supplied (design
# "Orchestrator interface" required-tool guard). An occasion result requires the
# catalog entry plus an occasion source (profile or a direct occasion).
_SIZE_REQUIRED_TOOLS: frozenset[str] = frozenset(
    {"get_sku_fit_signals", "get_product_catalog_entry", "get_sku_reviews"}
)
_OCCASION_REQUIRED_TOOLS: frozenset[str] = frozenset({"get_product_catalog_entry"})


# ---------------------------------------------------------------------------
# Validator-facing data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogView:
    """The catalog facts the validator needs (a slice of ProductCatalogEntry).

    ``offered_sizes`` is the SKU/style's offered size grid used by the offered-range
    clamp (Requirement 1.8); ``fit_guidance`` is retained for completeness but the
    validator does not re-derive size from it — it trusts the rubric for direction
    and only enforces the hard invariants.
    """

    offered_sizes: Sequence[float] = field(default_factory=tuple)
    fit_guidance: Optional[str] = None
    fit_return_rate: Optional[float] = None
    toe_box: Optional[str] = None


@dataclass(frozen=True)
class OmsView:
    """The OMS fit/exchange counts the validator needs.

    ``None`` for the whole object (``Signals.oms is None``) means the fit-history
    tool failed and the tool-failure short-circuit applies (Requirement 1.2). An
    OMS object that is present but whose ``signal_count`` is below the
    Minimum_Signal_Threshold marks a sparse (catalog-/review-driven) path.
    """

    too_big_count: int = 0
    too_small_count: int = 0
    uncomfortable_count: int = 0
    exchange_count: int = 0

    @property
    def signal_count(self) -> int:
        """Fit_Return_Signals + Exchange_Signals for the SKU (Requirements 1.3, 1.4)."""
        return (
            self.too_big_count
            + self.too_small_count
            + self.uncomfortable_count
            + self.exchange_count
        )


# Minimum_Signal_Threshold (Requirement 1.3 / Glossary): below this count the
# recommendation is on the sparse path and confidence is capped at medium.
MINIMUM_SIGNAL_THRESHOLD: int = 5


@dataclass(frozen=True)
class WidthSignals:
    """Width/shape inputs that may raise a warning but never change the size.

    Mirrors the design's width/comfort rule (Requirements 3.9, 3.11, 3.13): a
    warning fires if the SKU's reviews show a narrow/wide majority (>= 3 reviews),
    any customer width fit flag is set, or the catalog toe box is narrow/roomy.
    """

    # Review_Width_Signals majority ("narrow"/"wide") with >= 3 reviews.
    review_width_count: int = 0
    review_width_majority: Optional[str] = None  # "narrow" | "wide" | "normal" | None
    # Customer_Fit_Profile width flags (Requirement 3.11).
    customer_too_narrow: bool = False
    customer_too_wide: bool = False
    customer_tight_toes: bool = False
    customer_heels_slip: bool = False
    # Catalog_Toe_Box (Requirement 3.13).
    toe_box: Optional[str] = None  # "regular" | "narrow" | "roomy" | None


@dataclass(frozen=True)
class Signals:
    """Everything the validator reasons over, assembled from the tool results.

    This is the ``signals`` argument of the design pseudocode. It carries exactly
    the legitimate fit inputs (Requirement 10.1) plus the bookkeeping the hard
    invariants need: which tools were invoked, the request type, and whether any
    protected attribute leaked through.
    """

    request_type: str
    invoked_tools: frozenset[str]
    stated_usual_size: float
    sizing_system: str
    catalog: CatalogView
    oms: Optional[OmsView] = None
    foot_length_cm: Optional[float] = None
    # True only when a customer id was supplied (then get_customer_profile is
    # mandatory for a size recommendation; design required-tool guard).
    customer_id_supplied: bool = False
    width: WidthSignals = field(default_factory=WidthSignals)
    # Outcome_Fit_Signals count — reinforces and is counted toward the supporting
    # signal total (Requirement 4.4); it does not gate the OMS threshold.
    outcome_signal_count: int = 0
    # Keys of any Protected_Attribute that reached the validator; non-empty => reject.
    protected_attributes_present: frozenset[str] = frozenset()

    @property
    def supporting_signal_count(self) -> int:
        """Supporting Fit_Return_Signals + Exchange_Signals + Outcome_Fit_Signals.

        Reported with the recommendation (Requirement 1.10) and includes outcome
        signals, which reinforce rather than override (Requirement 4.4).
        """
        oms_count = self.oms.signal_count if self.oms is not None else 0
        return oms_count + self.outcome_signal_count


@dataclass(frozen=True)
class ProposedDecision:
    """Claude's structured output (design "Structured Output Contract").

    ``recommended_size`` and ``confidence_level`` are what the validator checks and
    corrects; the rationale and cited signals pass through unchanged; width/comfort
    hints are advisory only (the validator recomputes warnings from ``signals``).
    """

    recommended_size: float
    confidence_level: str
    size_rationale: str = ""
    cited_signals: Sequence[str] = field(default_factory=tuple)
    width_warning: Optional[str] = None
    comfort_note: Optional[str] = None


@dataclass(frozen=True)
class Correction:
    """A single clamp/reject the validator applied, for the Reasoning_Trace.

    ``invariant`` names which guardrail fired; ``proposed_value`` /
    ``corrected_value`` capture the before/after so the override is transparent
    (design ReasoningTrace ``validator_corrections``).
    """

    invariant: str
    proposed_value: object
    corrected_value: object


@dataclass(frozen=True)
class ValidatedDecision:
    """The final, invariant-guaranteed decision the orchestrator emits.

    ``supporting_signal_count`` is surfaced here so the recommendation can report
    the count of supporting signals it used (Requirements 1.10, 4.4).
    """

    size: float
    confidence: str
    warnings: List[str] = field(default_factory=list)
    rationale: str = ""
    cited_signals: Sequence[str] = field(default_factory=tuple)
    corrections: List[Correction] = field(default_factory=list)
    supporting_signal_count: int = 0
    note: Optional[str] = None


@dataclass(frozen=True)
class RejectedDecision:
    """A recommendation the validator refused to emit (not presented to the customer).

    Produced when a hard precondition fails: a required tool was not invoked
    (Requirement 9.7) or a Protected_Attribute reached the validator
    (Requirements 10.1, 10.2, 10.6).
    """

    reason: str


# The validator returns either a compliant decision or a rejection.
ValidationResult = Union[ValidatedDecision, RejectedDecision]


# ---------------------------------------------------------------------------
# Guard helpers
# ---------------------------------------------------------------------------


def required_tools_called(invoked_tools: frozenset[str], request_type: str,
                          customer_id_supplied: bool) -> bool:
    """Return whether the mandatory tools for ``request_type`` were all invoked.

    A size recommendation needs fit signals, the catalog entry, and reviews; when a
    customer id was supplied it also needs the customer profile. An occasion result
    needs the catalog entry (the occasion source is the profile tool or a directly
    supplied occasion, handled by the orchestrator). Any other request type has no
    required tools here (Requirement 9.7).
    """
    if request_type == REQUEST_SIZE:
        required = set(_SIZE_REQUIRED_TOOLS)
        if customer_id_supplied:
            required.add("get_customer_profile")
        return required.issubset(invoked_tools)
    if request_type == REQUEST_OCCASION:
        return _OCCASION_REQUIRED_TOOLS.issubset(invoked_tools)
    return True


def has_protected_attributes(signals: Signals) -> bool:
    """Return whether any recognized Protected_Attribute reached the validator.

    Only the five canonical Protected_Attributes count (Requirement 10.2 /
    Glossary Protected_Attribute); the legitimate ``sizing_system`` and product
    ``gender_category`` are never in this set (Requirements 10.5, 10.7).
    """
    return bool(set(signals.protected_attributes_present) & PROTECTED_ATTRIBUTES)


def _is_sparse(signals: Signals) -> bool:
    """Return whether this is a sparse (catalog-/review-driven) recommendation.

    The sparse path is active when OMS Fit_Return_Signals + Exchange_Signals are
    below the Minimum_Signal_Threshold; such recommendations are capped at
    ``medium`` confidence (Requirements 1.19, 3.4).
    """
    oms_count = signals.oms.signal_count if signals.oms is not None else 0
    return oms_count < MINIMUM_SIGNAL_THRESHOLD


def width_warnings(signals: Signals) -> List[str]:
    """Recompute width/shape warnings from the signals (never touch the size).

    A single combined width warning fires when any one source indicates a concern
    (Requirements 3.9, 3.11, 3.13):

    * reviews: >= 3 Review_Width_Signals with a ``narrow``/``wide`` majority;
    * customer: any of too narrow / too wide / tight toes / heels slip;
    * catalog: a ``narrow`` or ``roomy`` toe box.

    The warning is recomputed here rather than trusting the proposal, so a
    width/comfort hint can never have influenced the size.
    """
    width = signals.width
    reasons: List[str] = []

    if (
        width.review_width_count >= 3
        and width.review_width_majority in ("narrow", "wide")
    ):
        reasons.append(f"reviews report this style runs {width.review_width_majority}")

    if width.customer_too_narrow or width.customer_too_wide:
        reasons.append("your fit profile notes a width concern")
    if width.customer_tight_toes:
        reasons.append("your fit profile notes tightness around the toes")
    if width.customer_heels_slip:
        reasons.append("your fit profile notes heel slippage")

    if width.toe_box in ("narrow", "roomy"):
        reasons.append(f"this style has a {width.toe_box} toe box")

    if not reasons:
        return []
    return ["Width/shape note: " + "; ".join(reasons) + "."]


# ---------------------------------------------------------------------------
# The validator
# ---------------------------------------------------------------------------


def validate(proposed: ProposedDecision, signals: Signals) -> ValidationResult:
    """Enforce the hard invariants on Claude's proposed decision.

    Pure and deterministic (no I/O, no randomness): identical ``(proposed,
    signals)`` always yields an identical result (Requirement 10.4). Returns a
    :class:`ValidatedDecision` with every clamp recorded, or a
    :class:`RejectedDecision` when the recommendation cannot be made compliant.

    The steps follow the design pseudocode exactly, in order.
    """
    corrections: List[Correction] = []

    # Invariant 5 — required-tool guard (Req 9.7): reject if a mandatory tool for
    # this request type was not invoked.
    if not required_tools_called(
        signals.invoked_tools, signals.request_type, signals.customer_id_supplied
    ):
        return RejectedDecision(reason="required tool not called")

    # Invariant 4 (protected attrs absent, Req 10.1/10.2/10.6): reject if any
    # Protected_Attribute reached the validator.
    if has_protected_attributes(signals):
        return RejectedDecision(reason="protected attribute present")

    # Tool-failure short-circuit (Req 1.2): fit history could not be retrieved ->
    # recommend the Stated_Usual_Size at low confidence with an explicit note. The
    # size is still snapped to the offered range so the result is presentable.
    if signals.oms is None:
        size = signals.stated_usual_size
        if signals.catalog.offered_sizes:
            size = clamp_to_offered(size, list(signals.catalog.offered_sizes))
        return ValidatedDecision(
            size=size,
            confidence=LOW,
            warnings=width_warnings(signals),
            rationale=proposed.size_rationale,
            cited_signals=tuple(proposed.cited_signals),
            corrections=corrections,
            supporting_signal_count=signals.supporting_signal_count,
            note="fit history could not be retrieved",
        )

    size = proposed.recommended_size
    confidence = proposed.confidence_level

    # Invariant 2 — measurement precedence (Req 1.11, 1.15): when a Foot_Measurement
    # is present, the measurement lookup sets the size regardless of the proposal.
    if signals.foot_length_cm is not None:
        corrected = measurement_lookup(signals.foot_length_cm, signals.sizing_system)
        if corrected != size:
            corrections.append(
                Correction("measurement_precedence", size, corrected)
            )
            size = corrected
    else:
        # Invariant 1 — net single half-size cap (Req 1.5/1.6/1.12/1.13/1.19): a
        # non-measurement shift larger than one half size is clamped back to one
        # half size in the proposed direction.
        steps = half_steps_between(size, signals.stated_usual_size)
        if abs(steps) > 1:
            clamped_steps = 1 if steps > 0 else -1
            clamped_size = apply_half_steps(signals.stated_usual_size, clamped_steps)
            corrections.append(Correction("half_size_cap", size, clamped_size))
            size = clamped_size

    # Invariant 6 — offered-range clamp (Req 1.8): snap to the nearest offered size.
    if signals.catalog.offered_sizes:
        snapped = clamp_to_offered(size, list(signals.catalog.offered_sizes))
        if snapped != size:
            corrections.append(Correction("offered_range", size, snapped))
        size = snapped

    # Invariant 3 — confidence bounds + sparse cap (Req 1.4/1.17-1.20/3.4): clamp
    # into {low, medium, high}, then cap sparse (OMS-below-threshold) paths at medium.
    confidence = clamp_tier(confidence)
    if _is_sparse(signals) and TIER_INDEX[confidence] > TIER_INDEX[MEDIUM]:
        corrections.append(
            Correction("sparse_confidence_cap", confidence, MEDIUM)
        )
        confidence = MEDIUM

    # Invariant 4 — width/comfort are warning-only (Req 3.9/3.11/3.12/3.13):
    # recomputed from the signals; they never touch the size.
    warnings = width_warnings(signals)

    return ValidatedDecision(
        size=size,
        confidence=confidence,
        warnings=warnings,
        rationale=proposed.size_rationale,
        cited_signals=tuple(proposed.cited_signals),
        corrections=corrections,
        supporting_signal_count=signals.supporting_signal_count,
    )
