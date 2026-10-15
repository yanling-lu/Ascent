"""Reasoning Trace Builder — ordered trace assembly with identifier masking (Task 11.1).

This module assembles the design's ``ReasoningTrace`` (design "Data Models" ->
``ReasoningTrace``) from the agent's run. It records, **in execution order**, each
reasoning step, each named Agent_Tool invoked (with the data it retrieved) and the
resulting decision; it records every correction the deterministic Guardrail
Validator applied; it records any Agent_Tool that failed and the data that was
therefore unavailable; and it emits two views — a plain-language, customer-facing
explanation that cites at least one historical signal, and a full trace for a
Human_Reviewer.

The whole trace is **masked** so a customer cannot be re-identified from it: the
two direct identifiers ``customer_uid`` and ``cnsmr_prof_sid`` are redacted
wherever they appear in any recorded data (Requirements 7.5, 10.3). Masking is
applied as data is recorded, so no unmasked identifier is ever retained on the
trace object.

The builder is a pure assembler: it performs no I/O and no network calls. It is
driven by the orchestrator, which calls :meth:`ReasoningTraceBuilder.record_step`,
:meth:`record_tool_invocation`, :meth:`record_tool_failure`, and
:meth:`record_validator_correction` as the run proceeds, then
:meth:`build` to produce the finished :class:`ReasoningTrace`.

Design references:
* Data Models — ``ReasoningTrace`` (``steps``, ``validator_corrections``,
  ``customer_facing_explanation``, ``full_trace``, ``masked_fields``).
* Component "Reasoning Trace Builder — identifier masking".
* Requirements 7.1 (ordered steps/tools/data/decision), 7.2 (plain-language
  explanation citing >=1 historical signal), 7.5 / 10.3 (mask customer_uid and
  cnsmr_prof_sid), 7.6 (record failed tools + unavailable data), 11.5 (full view
  for the Human_Reviewer).

The validator's :class:`ai_fit.reasoning.validator.Correction` dataclass is reused
verbatim for recording validator corrections, so a correction the validator
produced flows into the trace without re-shaping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

# Reuse the validator's Correction dataclass (fields: invariant, proposed_value,
# corrected_value) for recording validator corrections — the design's
# ``validator_corrections`` entries are exactly these.
from ai_fit.reasoning.validator import Correction

# ---------------------------------------------------------------------------
# Masking (Requirements 7.5, 10.3)
# ---------------------------------------------------------------------------

# The two direct customer identifiers that must always be masked out of the
# Reasoning_Trace so an individual customer cannot be re-identified from it
# (Requirements 7.5, 10.3). These are the exact field names named in the
# requirements and in the design's ``masked_fields`` list.
MASKED_FIELDS: tuple[str, ...] = ("customer_uid", "cnsmr_prof_sid")

# The redaction placeholder written in place of a masked identifier's value. A
# fixed, value-independent token guarantees no fragment of the original identifier
# survives (so it cannot be used to re-identify the customer).
MASK_PLACEHOLDER: str = "***MASKED***"


def _mask_value(value: Any) -> Any:
    """Recursively mask any masked-field keys inside ``value``.

    Walks mappings and sequences so a masked identifier is redacted no matter how
    deeply it is nested in the data an Agent_Tool returned. For a mapping, any key
    in :data:`MASKED_FIELDS` has its value replaced with :data:`MASK_PLACEHOLDER`;
    other values are masked recursively. Lists/tuples are walked element-wise.
    Scalars pass through unchanged. The returned structure is a copy — the caller's
    original data is never mutated — so recording data on the trace cannot alter the
    live records the orchestrator still holds.
    """
    if isinstance(value, Mapping):
        masked: Dict[Any, Any] = {}
        for key, inner in value.items():
            if key in MASKED_FIELDS:
                masked[key] = MASK_PLACEHOLDER
            else:
                masked[key] = _mask_value(inner)
        return masked
    # Mask inside sequences, but never treat a string/bytes as a sequence to walk.
    if isinstance(value, (list, tuple)):
        return [_mask_value(item) for item in value]
    return value


def mask_identifiers(data: Any) -> Any:
    """Return a deep copy of ``data`` with ``customer_uid`` / ``cnsmr_prof_sid`` redacted.

    This is the single masking entry point the builder applies to everything it
    records (step data, tool outputs, failure context). Any occurrence of one of
    the two direct identifiers (:data:`MASKED_FIELDS`), at any nesting depth, has
    its value replaced with :data:`MASK_PLACEHOLDER` (Requirements 7.5, 10.3).
    """
    return _mask_value(data)


# ---------------------------------------------------------------------------
# Step types (ordered trace entries)
# ---------------------------------------------------------------------------

# The kinds of entry that appear in the ordered ``steps`` list. ``reasoning`` is a
# model reasoning step; ``tool_invocation`` is a named Agent_Tool call with the data
# it retrieved; ``tool_failure`` records an Agent_Tool that failed and the data that
# was therefore unavailable (Requirement 7.6); ``decision`` is a resulting decision.
STEP_REASONING: str = "reasoning"
STEP_TOOL_INVOCATION: str = "tool_invocation"
STEP_TOOL_FAILURE: str = "tool_failure"
STEP_DECISION: str = "decision"


@dataclass(frozen=True)
class TraceStep:
    """One ordered entry in the Reasoning_Trace (design ``steps`` element).

    Mirrors the design's step shape ``{order, step_type, tool_name?, data_summary,
    decision}``:

    * ``order`` — 1-based position in execution order (Requirement 7.1).
    * ``step_type`` — one of :data:`STEP_REASONING`, :data:`STEP_TOOL_INVOCATION`,
      :data:`STEP_TOOL_FAILURE`, :data:`STEP_DECISION`.
    * ``tool_name`` — the named Agent_Tool for tool steps (``None`` for pure
      reasoning/decision steps).
    * ``data_summary`` — the data retrieved / observed at this step, already masked.
    * ``decision`` — the resulting decision/outcome recorded at this step.

    Instances are immutable; data placed here has already passed through
    :func:`mask_identifiers`.
    """

    order: int
    step_type: str
    data_summary: Any = None
    tool_name: Optional[str] = None
    decision: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        """Return the step as a plain dict for the full-trace view."""
        return {
            "order": self.order,
            "step_type": self.step_type,
            "tool_name": self.tool_name,
            "data_summary": self.data_summary,
            "decision": self.decision,
        }


@dataclass(frozen=True)
class ReasoningTrace:
    """The finished Reasoning_Trace (design Data Models ``ReasoningTrace``).

    * ``steps`` — the ordered reasoning/tool/decision entries (Requirement 7.1).
    * ``validator_corrections`` — every clamp/reject the Guardrail Validator
      applied, as :class:`Correction` records (design ``validator_corrections``).
    * ``customer_facing_explanation`` — plain-language, cites >=1 historical signal
      (Requirement 7.2).
    * ``failed_tools`` — the Agent_Tools that failed and the data left unavailable
      (Requirement 7.6).
    * ``masked_fields`` — the identifiers that are always masked out of the trace
      (Requirements 7.5, 10.3).

    :meth:`full_trace` renders the complete view for a Human_Reviewer
    (Requirement 11.5).
    """

    steps: Sequence[TraceStep] = field(default_factory=tuple)
    validator_corrections: Sequence[Correction] = field(default_factory=tuple)
    customer_facing_explanation: str = ""
    failed_tools: Sequence["ToolFailure"] = field(default_factory=tuple)
    masked_fields: tuple[str, ...] = MASKED_FIELDS

    def full_trace(self) -> Dict[str, Any]:
        """Return the full, Human_Reviewer-facing view of the trace (Requirement 11.5).

        Surfaces everything: every ordered step, every validator correction, every
        failed tool with its unavailable data, the customer-facing explanation, and
        the list of masked fields. Identifiers are already masked in every recorded
        value (Requirements 7.5, 10.3), so even the full reviewer view cannot
        re-identify a customer.
        """
        return {
            "steps": [step.as_dict() for step in self.steps],
            "validator_corrections": [
                {
                    "invariant": c.invariant,
                    "proposed_value": c.proposed_value,
                    "corrected_value": c.corrected_value,
                }
                for c in self.validator_corrections
            ],
            "customer_facing_explanation": self.customer_facing_explanation,
            "failed_tools": [
                {"tool_name": f.tool_name, "unavailable_data": f.unavailable_data}
                for f in self.failed_tools
            ],
            "masked_fields": list(self.masked_fields),
        }


@dataclass(frozen=True)
class ToolFailure:
    """A failed Agent_Tool and the data that was therefore unavailable (Requirement 7.6).

    ``tool_name`` is the named Agent_Tool that failed; ``unavailable_data`` names the
    data that could not be retrieved as a result, so the trace makes the gap
    explicit while the agent still produces a visible trace.
    """

    tool_name: str
    unavailable_data: str


# ---------------------------------------------------------------------------
# Historical-signal citation (Requirement 7.2)
# ---------------------------------------------------------------------------

# Human-readable names for the historical signal sources Claude cites in
# ``cited_signals`` (design "Structured Output Contract" / ``cited_signals[]``).
# Used to turn a cited-signal token into a plain-language phrase for the
# customer-facing explanation.
_HISTORICAL_SIGNAL_LABELS: Dict[str, str] = {
    "oms_fit_returns": "past fit-related returns for this style",
    "oms_exchanges": "past exchanges for this style",
    "customer_fit_profile": "your saved fit preferences",
    "catalog_fit_guidance": "the product's fit guidance",
    "review_fit_signals": "what other customers said about the fit",
    "review_width_signals": "what other customers said about the width",
    "outcome_fit_signals": "how our past size recommendations turned out",
    "foot_measurement": "your recorded foot measurement",
}

# The subset of cited-signal sources that count as a *historical* signal for the
# "cites at least one historical signal" requirement (Requirement 7.2). These are
# the data-derived signals (returns, exchanges, reviews, outcomes, measurement),
# as opposed to the customer's own stated inputs.
HISTORICAL_SIGNAL_SOURCES: frozenset[str] = frozenset(
    {
        "oms_fit_returns",
        "oms_exchanges",
        "catalog_fit_guidance",
        "review_fit_signals",
        "review_width_signals",
        "outcome_fit_signals",
        "foot_measurement",
    }
)


def _label_for_signal(signal: str) -> str:
    """Return a plain-language label for a cited-signal token.

    Falls back to a readable form of the raw token (underscores -> spaces) when the
    token is not in :data:`_HISTORICAL_SIGNAL_LABELS`, so an unexpected signal name
    still reads sensibly in the explanation rather than leaking a raw identifier.
    """
    return _HISTORICAL_SIGNAL_LABELS.get(signal, signal.replace("_", " "))


def build_customer_facing_explanation(
    rationale: str,
    cited_signals: Sequence[str],
    confidence: str,
) -> str:
    """Compose the plain-language, customer-facing explanation (Requirement 7.2).

    The explanation leads with the model's ``rationale`` (already plain language),
    then names at least one supporting **historical** signal so Requirement 7.2 is
    satisfied, and closes with the confidence level the customer sees. If
    ``cited_signals`` includes a historical source, its plain-language label is
    cited; if none is present, the explanation says the guidance is based on the
    customer's stated size — making the absence of history explicit rather than
    implying a signal that was not used.

    The returned text contains no direct identifiers: it is built only from the
    rationale, the signal labels, and the confidence tier, none of which carry
    ``customer_uid`` / ``cnsmr_prof_sid``.
    """
    parts: List[str] = []
    rationale = (rationale or "").strip()
    if rationale:
        parts.append(rationale)

    historical = [s for s in cited_signals if s in HISTORICAL_SIGNAL_SOURCES]
    if historical:
        labels = [_label_for_signal(s) for s in historical]
        if len(labels) == 1:
            cite = labels[0]
        elif len(labels) == 2:
            cite = f"{labels[0]} and {labels[1]}"
        else:
            cite = ", ".join(labels[:-1]) + f", and {labels[-1]}"
        parts.append(f"This is based on {cite}.")
    else:
        parts.append("This is based on your stated usual size.")

    confidence = (confidence or "").strip().lower()
    if confidence:
        parts.append(f"Confidence in this recommendation is {confidence}.")

    return " ".join(parts)


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------


@dataclass
class ReasoningTraceBuilder:
    """Assembles a :class:`ReasoningTrace` in execution order (Requirement 7.1).

    The orchestrator drives the builder as the run proceeds:

    * :meth:`record_step` — a reasoning step with a decision.
    * :meth:`record_tool_invocation` — a named Agent_Tool call and the data it
      returned.
    * :meth:`record_tool_failure` — a failed Agent_Tool and the data left
      unavailable (Requirement 7.6).
    * :meth:`record_validator_correction` — a correction the Guardrail Validator
      applied (design ``validator_corrections``).
    * :meth:`build` — produce the finished trace, composing the customer-facing
      explanation (Requirement 7.2).

    Every piece of recorded data passes through :func:`mask_identifiers` before it
    is stored, so no unmasked ``customer_uid`` / ``cnsmr_prof_sid`` ever lands on
    the trace (Requirements 7.5, 10.3). Steps are appended in call order, and each
    is stamped with a monotonically increasing 1-based ``order`` so the trace
    reflects true execution order.
    """

    _steps: List[TraceStep] = field(default_factory=list)
    _corrections: List[Correction] = field(default_factory=list)
    _failures: List[ToolFailure] = field(default_factory=list)

    @property
    def _next_order(self) -> int:
        """Return the next 1-based order index for an appended step."""
        return len(self._steps) + 1

    def record_step(
        self, data_summary: Any = None, decision: Optional[str] = None
    ) -> "ReasoningTraceBuilder":
        """Record a reasoning step with its observed data and resulting decision.

        ``data_summary`` is masked before being stored. Returns ``self`` so calls
        can be chained. (Requirement 7.1 — reasoning step in execution order.)
        """
        self._steps.append(
            TraceStep(
                order=self._next_order,
                step_type=STEP_REASONING,
                data_summary=mask_identifiers(data_summary),
                decision=decision,
            )
        )
        return self

    def record_tool_invocation(
        self,
        tool_name: str,
        data_summary: Any = None,
        decision: Optional[str] = None,
    ) -> "ReasoningTraceBuilder":
        """Record a named Agent_Tool invocation and the data it retrieved.

        ``tool_name`` is the Agent_Tool the model invoked; ``data_summary`` is the
        data it returned (masked before storage). (Requirement 7.1 — each named
        Agent_Tool invoked and the data retrieved, in execution order.)
        """
        self._steps.append(
            TraceStep(
                order=self._next_order,
                step_type=STEP_TOOL_INVOCATION,
                tool_name=tool_name,
                data_summary=mask_identifiers(data_summary),
                decision=decision,
            )
        )
        return self

    def record_decision(
        self, decision: str, data_summary: Any = None
    ) -> "ReasoningTraceBuilder":
        """Record a resulting decision as an ordered step (Requirement 7.1)."""
        self._steps.append(
            TraceStep(
                order=self._next_order,
                step_type=STEP_DECISION,
                data_summary=mask_identifiers(data_summary),
                decision=decision,
            )
        )
        return self

    def record_tool_failure(
        self, tool_name: str, unavailable_data: str
    ) -> "ReasoningTraceBuilder":
        """Record a failed Agent_Tool and the data left unavailable (Requirement 7.6).

        The failure is recorded both as an ordered step (so the gap appears in
        execution order) and in the dedicated ``failed_tools`` list surfaced to the
        Human_Reviewer. The agent still produces a visible trace despite the failure.
        """
        self._failures.append(
            ToolFailure(tool_name=tool_name, unavailable_data=unavailable_data)
        )
        self._steps.append(
            TraceStep(
                order=self._next_order,
                step_type=STEP_TOOL_FAILURE,
                tool_name=tool_name,
                data_summary={"unavailable_data": unavailable_data},
                decision=f"{tool_name} failed; {unavailable_data} unavailable",
            )
        )
        return self

    def record_validator_correction(
        self, correction: Correction
    ) -> "ReasoningTraceBuilder":
        """Record a correction the Guardrail Validator applied.

        Reuses the validator's :class:`Correction` dataclass verbatim so a
        correction the validator produced is stored without re-shaping (design
        ``validator_corrections``).
        """
        self._corrections.append(correction)
        return self

    def record_validator_corrections(
        self, corrections: Sequence[Correction]
    ) -> "ReasoningTraceBuilder":
        """Record every correction from a validator result (convenience over the singular)."""
        for correction in corrections:
            self.record_validator_correction(correction)
        return self

    def build(
        self,
        rationale: str = "",
        cited_signals: Sequence[str] = (),
        confidence: str = "",
    ) -> ReasoningTrace:
        """Produce the finished :class:`ReasoningTrace`.

        Composes the plain-language, customer-facing explanation from the model's
        ``rationale``, the ``cited_signals`` (ensuring >=1 historical signal is
        named), and the ``confidence`` tier (Requirement 7.2), then freezes the
        ordered steps, validator corrections, and failed-tool records into an
        immutable trace. The ``masked_fields`` list is attached so a consumer can
        see which identifiers are redacted (Requirements 7.5, 10.3).
        """
        explanation = build_customer_facing_explanation(
            rationale=rationale,
            cited_signals=cited_signals,
            confidence=confidence,
        )
        return ReasoningTrace(
            steps=tuple(self._steps),
            validator_corrections=tuple(self._corrections),
            customer_facing_explanation=explanation,
            failed_tools=tuple(self._failures),
            masked_fields=MASKED_FIELDS,
        )
