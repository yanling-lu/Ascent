"""The Bedrock Converse tool-use loop at temperature 0 (Task 12.2).

This is the design's **Agent Orchestrator** (design "Architecture" ->
``Agent Orchestrator — Bedrock Converse loop``; "Separation of concerns" ->
*Orchestrator*). It wires together every component the earlier tasks built:

* the rubric system prompt and the Structured Output Contract
  (:mod:`ai_fit.reasoning.rubric`),
* the ``toolConfig`` describing the 10 Agent_Tools (:mod:`ai_fit.tools`),
* the :class:`~ai_fit.orchestrator.dispatcher.ToolDispatcher` (timeout + retry +
  invoked-tool tracking + the required-tool guard),
* Claude's structured decision via the Converse API,
* the structured-output parser (:mod:`ai_fit.reasoning.parser`),
* the deterministic Guardrail Validator (:mod:`ai_fit.reasoning.validator`),
* the Reasoning_Trace builder with identifier masking
  (:mod:`ai_fit.trace.builder`), and
* human routing for direct financial requests
  (:mod:`ai_fit.tools.human_routing`).

The loop (design "Request lifecycle" sequence diagram):

1. Call ``Converse`` with ``system`` = the rubric prompt, ``toolConfig`` = the 10
   tool specs, and ``inferenceConfig.temperature = 0`` (Requirements 9.1, 9.3).
2. While Claude's stop reason is ``tool_use``, dispatch each ``toolUse`` block
   through the :class:`ToolDispatcher` (10s timeout, up to 2 retries) and feed the
   results back as a ``toolResult`` user message. Tool results are the only way
   data reaches Claude — datasets are never stuffed into the prompt (Requirement
   9.3). The availability proxy (catalog-present = available) is already applied by
   the catalog/alternatives tools (Requirement 1.9).
3. When Claude returns the final structured decision, parse it, hand it to the
   Guardrail Validator, build + mask the Reasoning_Trace, and return a
   :class:`Recommendation` carrying the confidence, the customer-facing
   explanation, the supporting-signal count (Requirement 1.10), and the masked
   trace. A low-confidence recommendation is flagged ``advisory`` (Requirement
   11.4).

Error handling (design "Error Handling" table):

* **Bedrock throttling** — retry the Converse invocation at most
  :data:`ai_fit.config.TOOL_MAX_RETRIES` times, then return a failure
  :class:`Recommendation` (Requirement 9.5).
* **Bedrock non-throttling error** — return a failure immediately (Requirement
  9.4).
* **Required-tool guard** — if the validator rejects the recommendation because a
  mandatory tool was not called, it is not presented (Requirement 9.7).

Direct financial requests (refund / price change / discount / order modification)
are **declined and routed** to a Human_Reviewer via
:func:`handle_financial_request`; the agent never acts on them (Requirements 11.2,
11.3, 11.4).

Testability — the Bedrock call sits behind the thin :class:`BedrockConverseClient`
seam (a protocol + a boto3-backed default). The integration tests (Task 12.3) mock
this seam, so the loop can be exercised with no real AWS calls.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence

from ai_fit.config import (
    BEDROCK_MODEL_ID,
    BEDROCK_REGION,
    BEDROCK_TEMPERATURE,
    TOOL_MAX_RETRIES,
)
from ai_fit.reasoning.parser import try_parse_proposed_decision
from ai_fit.reasoning.rubric import STRUCTURED_OUTPUT_SCHEMA, build_system_prompt
from ai_fit.reasoning.sizing import LOW
from ai_fit.reasoning.validator import (
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
from ai_fit.tools.human_routing import AuditLog, route_to_human_reviewer
from ai_fit.trace.builder import ReasoningTraceBuilder

__all__ = [
    "BedrockConverseClient",
    "BotoBedrockConverseClient",
    "ThrottlingError",
    "ToolSpec",
    "Recommendation",
    "ConverseOrchestrator",
    "build_tool_config",
    "FINANCIAL_ACTION_TYPES",
    "handle_financial_request",
]


# ---------------------------------------------------------------------------
# Bedrock client seam (testable with a mocked runtime)
# ---------------------------------------------------------------------------


class ThrottlingError(Exception):
    """Raised by a :class:`BedrockConverseClient` for a Bedrock throttling error.

    The orchestrator distinguishes throttling (retry at most
    :data:`TOOL_MAX_RETRIES` times, Requirement 9.5) from every other error
    (immediate failure, Requirement 9.4). The boto3-backed client raises this when
    the runtime returns ``ThrottlingException`` / ``TooManyRequestsException``;
    mocked clients in tests raise it directly to exercise the retry path.
    """


class BedrockConverseClient(Protocol):
    """Thin seam over the Bedrock Runtime ``Converse`` call.

    Keeping Bedrock behind this protocol means the Converse loop never imports
    boto3 directly and the integration tests (Task 12.3) can supply a mock that
    returns canned Converse responses or raises :class:`ThrottlingError`. A single
    method mirrors the ``bedrock-runtime`` ``converse`` API shape.
    """

    def converse(
        self,
        *,
        model_id: str,
        messages: List[Dict[str, Any]],
        system: List[Dict[str, Any]],
        tool_config: Dict[str, Any],
        temperature: float,
    ) -> Dict[str, Any]:
        """Invoke Converse and return the raw response dict.

        Implementations must raise :class:`ThrottlingError` for a throttling
        condition and any other exception for a non-throttling error, so the
        orchestrator can apply the two distinct handling paths (Requirements 9.4,
        9.5).
        """
        ...


class BotoBedrockConverseClient:
    """boto3-backed :class:`BedrockConverseClient` for real Bedrock calls.

    Lazily constructs a ``bedrock-runtime`` client (boto3 is imported inside the
    constructor so merely importing this module — e.g. during test collection — does
    not require boto3 or AWS credentials). Translates the runtime's throttling error
    codes into :class:`ThrottlingError` so the orchestrator's retry path (Requirement
    9.5) fires, and lets every other error propagate for the immediate-failure path
    (Requirement 9.4).
    """

    # Bedrock/botocore error codes that mean "throttled" (retryable per Req 9.5).
    _THROTTLING_CODES = frozenset(
        {"ThrottlingException", "TooManyRequestsException", "Throttling"}
    )

    def __init__(self, region_name: str = BEDROCK_REGION, client: Any = None) -> None:
        if client is not None:
            self._client = client
        else:
            import boto3  # imported lazily so import of this module is cheap

            self._client = boto3.client("bedrock-runtime", region_name=region_name)

    def converse(
        self,
        *,
        model_id: str,
        messages: List[Dict[str, Any]],
        system: List[Dict[str, Any]],
        tool_config: Dict[str, Any],
        temperature: float,
    ) -> Dict[str, Any]:
        """Call the runtime ``converse`` API, mapping throttling to :class:`ThrottlingError`."""
        try:
            return self._client.converse(
                modelId=model_id,
                messages=messages,
                system=system,
                toolConfig=tool_config,
                inferenceConfig={"temperature": temperature},
            )
        except Exception as exc:  # noqa: BLE001 - classify then re-raise
            code = _botocore_error_code(exc)
            if code in self._THROTTLING_CODES:
                raise ThrottlingError(str(exc)) from exc
            raise


def _botocore_error_code(exc: Exception) -> Optional[str]:
    """Best-effort extraction of a botocore ``ClientError`` error code.

    Returns the ``Error.Code`` string from a botocore ``ClientError`` response, or
    ``None`` when the exception does not carry that structure. Written without a
    hard botocore dependency so this module imports cleanly without boto3.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            code = error.get("Code")
            if isinstance(code, str):
                return code
    return None


# ---------------------------------------------------------------------------
# Tool specs + toolConfig
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """One Agent_Tool exposed to Claude through the Converse ``toolConfig``.

    * ``name`` — the tool name Claude invokes (matches the design tool catalog and
      the required-tool guard, e.g. ``get_sku_fit_signals``).
    * ``description`` — a short purpose line surfaced to the model.
    * ``input_schema`` — the JSON Schema for the tool's ``input`` block.
    * ``handler`` — a zero-or-more-arg callable the dispatcher runs; it receives the
      ``input`` dict Claude supplied as keyword arguments (already bound to its
      store/stores by the orchestrator when the spec was built).
    """

    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: Callable[..., Any]


def build_tool_config(tool_specs: Sequence[ToolSpec]) -> Dict[str, Any]:
    """Build the Converse ``toolConfig`` from the agent's tool specs.

    Produces the ``{"tools": [{"toolSpec": {name, description, inputSchema}}]}``
    structure the Converse API expects, one entry per Agent_Tool (Requirement 9.2 —
    one dedicated tool per data source). The handlers are not part of the wire
    payload; the orchestrator keeps them in a name->spec map to dispatch ``toolUse``
    requests.
    """
    return {
        "tools": [
            {
                "toolSpec": {
                    "name": spec.name,
                    "description": spec.description,
                    "inputSchema": {"json": spec.input_schema},
                }
            }
            for spec in tool_specs
        ]
    }


# A string-keyed input schema used for tools that take a single ``sku`` argument.
_SKU_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {"sku": {"type": "string", "description": "The product SKU."}},
    "required": ["sku"],
}


# ---------------------------------------------------------------------------
# Direct financial requests — decline + route (Requirements 11.2, 11.3, 11.4)
# ---------------------------------------------------------------------------

# The action types the agent has no authority to perform; a direct request for any
# of these is declined and routed to a Human_Reviewer (Requirements 11.1-11.3).
FINANCIAL_ACTION_TYPES: frozenset[str] = frozenset(
    {"refund", "price_change", "discount", "order_modification"}
)


@dataclass(frozen=True)
class HumanRoutingResult:
    """The outcome of declining + routing a direct financial request (Req 11.2/11.3).

    ``declined`` is always ``True`` (the agent never acts on a financial request),
    ``routed`` reflects that an audit entry was written, and ``audit_id`` /
    ``timestamp`` echo the :class:`~ai_fit.tools.human_routing.AuditLogEntry` the
    routing tool created (Requirement 11.6). ``message`` is the customer-facing
    decline + routing notice.
    """

    declined: bool
    routed: bool
    action_type: str
    audit_id: str
    timestamp: str
    message: str


def handle_financial_request(
    request: str,
    action_type: str,
    audit_log: AuditLog,
) -> HumanRoutingResult:
    """Decline a direct financial request and route it to a Human_Reviewer.

    The AI_Fit_Agent has no financial authority: a refund / price change / discount
    / order-modification request is never acted on (Requirement 11.1). This declines
    the request, routes the intent via
    :func:`ai_fit.tools.human_routing.route_to_human_reviewer` (which appends an
    audit entry — Requirement 11.6), and returns a :class:`HumanRoutingResult`
    carrying the decline notice and the audit id (Requirements 11.2, 11.3).

    ``action_type`` should be one of :data:`FINANCIAL_ACTION_TYPES`; any other value
    is still routed (the guard is conservative — anything the orchestrator treats as
    financial is declined), but is normalized to ``order_modification`` when
    unrecognized so the audit record carries a known action type.
    """
    normalized = action_type if action_type in FINANCIAL_ACTION_TYPES else "order_modification"
    routed = route_to_human_reviewer(
        triggering_request=request, action_type=normalized, audit_log=audit_log
    )
    message = (
        "I can't make account, order, or payment changes. I've routed your "
        f"request ({normalized.replace('_', ' ')}) to a human reviewer who can help."
    )
    return HumanRoutingResult(
        declined=True,
        routed=bool(routed.get("routed")),
        action_type=normalized,
        audit_id=str(routed.get("audit_id", "")),
        timestamp=str(routed.get("timestamp", "")),
        message=message,
    )


# ---------------------------------------------------------------------------
# The recommendation result the orchestrator returns
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Recommendation:
    """The customer-facing result of a size recommendation run.

    Carries everything the UI needs to present the recommendation and the trace:

    * ``ok`` — ``True`` when a recommendation was produced; ``False`` for a Bedrock
      failure (``9.4``/``9.5``) or a validator rejection (``9.7``).
    * ``recommended_size`` / ``confidence`` — the validated size and tier.
    * ``advisory`` — ``True`` when the recommendation is low confidence and should be
      presented as advisory (Requirement 11.4).
    * ``explanation`` — the plain-language, customer-facing explanation citing >= 1
      historical signal (Requirement 7.2).
    * ``supporting_signal_count`` — the count of supporting signals used, including
      outcome signals (Requirements 1.10, 4.4).
    * ``warnings`` — width/comfort warnings (never size-changing).
    * ``trace`` — the masked full Reasoning_Trace for a Human_Reviewer / judge
      (Requirements 7.4, 7.5, 10.3, 11.5).
    * ``failure_reason`` — set when ``ok`` is ``False`` (throttling, model error, or
      rejection); surfaced as the customer message in that case.
    """

    ok: bool
    recommended_size: Optional[float] = None
    confidence: Optional[str] = None
    advisory: bool = False
    explanation: str = ""
    supporting_signal_count: int = 0
    warnings: List[str] = field(default_factory=list)
    note: Optional[str] = None
    trace: Optional[Dict[str, Any]] = None
    failure_reason: Optional[str] = None


# Customer-facing failure messages (design "Error Handling").
_THROTTLED_MESSAGE = (
    "The recommendation service is busy right now and the recommendation could "
    "not be generated. Please try again shortly."
)
_MODEL_ERROR_MESSAGE = "The recommendation could not be generated at this time."
_PARSE_ERROR_MESSAGE = "The recommendation could not be generated at this time."
_REJECTED_MESSAGE = "A complete recommendation could not be produced for this request."


# ---------------------------------------------------------------------------
# The orchestrator
# ---------------------------------------------------------------------------


class ConverseOrchestrator:
    """Runs the Bedrock Converse tool-use loop and emits a :class:`Recommendation`.

    Construct one per agent configuration with a :class:`BedrockConverseClient` (the
    boto3-backed default, or a mock in tests) and the ordered :class:`ToolSpec`
    list for the 10 Agent_Tools. Each call to :meth:`produce_recommendation` runs a
    fresh Converse loop with its own :class:`ReasoningTraceBuilder` and
    :class:`ToolDispatcher`, so runs do not share mutable state.

    The loop never lets a tool failure or a malformed model response raise out of
    the orchestrator: tool failures are recorded in the trace and the loop
    continues with partial results (the validator / required-tool guard decide
    whether the result is presentable), and a parse failure degrades to a failure
    :class:`Recommendation`.
    """

    def __init__(
        self,
        client: BedrockConverseClient,
        tool_specs: Sequence[ToolSpec],
        *,
        model_id: str = BEDROCK_MODEL_ID,
        temperature: float = BEDROCK_TEMPERATURE,
        max_throttle_retries: int = TOOL_MAX_RETRIES,
        max_tool_turns: int = 12,
        dispatcher_factory: Optional[
            Callable[[ReasoningTraceBuilder], Any]
        ] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._tool_specs = list(tool_specs)
        self._tools_by_name = {spec.name: spec for spec in self._tool_specs}
        self._tool_config = build_tool_config(self._tool_specs)
        self._system = [{"text": build_system_prompt()}]
        self._model_id = model_id
        self._temperature = temperature
        self._max_throttle_retries = max(0, max_throttle_retries)
        # Hard cap on tool-use rounds so a model that never stops asking for tools
        # cannot spin the loop forever.
        self._max_tool_turns = max(1, max_tool_turns)
        self._dispatcher_factory = dispatcher_factory or _default_dispatcher_factory
        self._sleep = sleep

    # -- public API ---------------------------------------------------------

    def produce_recommendation(
        self,
        sku: str,
        *,
        stated_usual_size: Optional[float] = None,
        customer_id: Optional[str] = None,
        direct_occasion: Optional[str] = None,
        request_type: str = REQUEST_SIZE,
    ) -> Recommendation:
        """Run the Converse loop and return a validated :class:`Recommendation`.

        Builds the opening user message from the request parameters (the only
        prompt content; all data arrives through tool results — Requirement 9.3),
        drives the tool-use loop, parses Claude's structured decision, validates it,
        and assembles the masked trace. A Bedrock throttling condition retries at
        most :data:`max_throttle_retries` times then fails (Requirement 9.5); any
        other Bedrock error fails immediately (Requirement 9.4); a validator
        rejection yields a non-presented failure (Requirement 9.7).
        """
        trace = ReasoningTraceBuilder()
        dispatcher = self._dispatcher_factory(trace)

        messages: List[Dict[str, Any]] = [
            {"role": "user", "content": [{"text": self._opening_prompt(
                sku=sku,
                stated_usual_size=stated_usual_size,
                customer_id=customer_id,
                direct_occasion=direct_occasion,
            )}]}
        ]

        # Captured tool results, keyed by tool name, so the validator Signals can be
        # assembled from the data Claude actually read (not from the prompt).
        captured: Dict[str, Any] = {}

        final_message: Optional[Dict[str, Any]] = None
        for _turn in range(self._max_tool_turns):
            try:
                response = self._converse_with_retry(messages)
            except ThrottlingError:
                return self._failure(trace, _THROTTLED_MESSAGE, "bedrock_throttled")
            except Exception as exc:  # noqa: BLE001 - any non-throttle error -> fail (Req 9.4)
                return self._failure(
                    trace, _MODEL_ERROR_MESSAGE, f"bedrock_error: {exc}"
                )

            output_message = _output_message(response)
            messages.append(output_message)
            stop_reason = response.get("stopReason")

            if stop_reason == "tool_use":
                tool_result_blocks = self._run_tool_uses(
                    output_message, dispatcher, captured
                )
                # Feed the tool results back to Claude as a user turn.
                messages.append({"role": "user", "content": tool_result_blocks})
                continue

            # Any non-tool stop reason ends the loop with this as the final message.
            final_message = output_message
            break

        if final_message is None:
            # Ran out of tool-use turns without a final decision.
            return self._failure(
                trace, _MODEL_ERROR_MESSAGE, "max_tool_turns_exceeded"
            )

        return self._finalize(
            final_message=final_message,
            dispatcher=dispatcher,
            trace=trace,
            captured=captured,
            sku=sku,
            stated_usual_size=stated_usual_size,
            customer_id=customer_id,
            request_type=request_type,
        )

    # -- Converse call with throttling retry (Req 9.4, 9.5) -----------------

    def _converse_with_retry(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Call Converse, retrying only on throttling up to the retry budget.

        A throttling condition is retried at most :data:`max_throttle_retries`
        times (so at most ``1 + max_throttle_retries`` attempts), then the final
        :class:`ThrottlingError` propagates for the fail-message path (Requirement
        9.5). A non-throttling error propagates on the first occurrence so the
        caller fails immediately (Requirement 9.4).
        """
        attempts = 1 + self._max_throttle_retries
        last_exc: Optional[ThrottlingError] = None
        for attempt in range(1, attempts + 1):
            try:
                return self._client.converse(
                    model_id=self._model_id,
                    messages=messages,
                    system=self._system,
                    tool_config=self._tool_config,
                    temperature=self._temperature,
                )
            except ThrottlingError as exc:
                last_exc = exc
                if attempt < attempts:
                    # Brief backoff between throttled attempts; ``sleep`` is a seam
                    # tests override to run instantly.
                    self._sleep(_backoff_seconds(attempt))
                    continue
                raise
        # Unreachable (the loop either returns or raises), but keeps mypy happy.
        assert last_exc is not None
        raise last_exc

    # -- tool-use dispatch --------------------------------------------------

    def _run_tool_uses(
        self,
        output_message: Dict[str, Any],
        dispatcher: Any,
        captured: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Dispatch every ``toolUse`` block in ``output_message`` and build results.

        Each ``toolUse`` is run through the :class:`ToolDispatcher` (timeout +
        retry + invoked-tool tracking). A successful call's value is captured (for
        Signals assembly) and returned to Claude as a ``toolResult`` block; a failed
        call returns a ``toolResult`` flagged ``status: "error"`` so Claude can
        continue with partial data (the trace already records the failure, Req 7.6).
        """
        result_blocks: List[Dict[str, Any]] = []
        for block in output_message.get("content", []):
            tool_use = block.get("toolUse")
            if not tool_use:
                continue
            name = tool_use.get("name", "")
            use_id = tool_use.get("toolUseId", "")
            tool_input = tool_use.get("input") or {}

            spec = self._tools_by_name.get(name)
            if spec is None:
                # Unknown tool name: return an error result rather than crashing.
                result_blocks.append(
                    _tool_result_block(use_id, {"error": f"unknown tool {name!r}"}, error=True)
                )
                continue

            result = dispatcher.dispatch(name, spec.handler, **tool_input)
            if result.ok:
                captured[name] = result.value
                result_blocks.append(_tool_result_block(use_id, result.value))
            else:
                result_blocks.append(
                    _tool_result_block(
                        use_id,
                        {"error": result.error or "tool failed", "available": False},
                        error=True,
                    )
                )
        return result_blocks

    # -- finalize: parse -> validate -> trace -> Recommendation -------------

    def _finalize(
        self,
        *,
        final_message: Dict[str, Any],
        dispatcher: Any,
        trace: ReasoningTraceBuilder,
        captured: Dict[str, Any],
        sku: str,
        stated_usual_size: Optional[float],
        customer_id: Optional[str],
        request_type: str,
    ) -> Recommendation:
        """Parse the structured decision, validate it, and assemble the result."""
        raw = _final_text(final_message)
        parsed = try_parse_proposed_decision(raw)
        if not parsed.ok or parsed.decision is None:
            return self._failure(trace, _PARSE_ERROR_MESSAGE, "unparseable_decision")

        proposed = parsed.decision
        trace.record_decision(
            decision="Claude proposed a structured size decision",
            data_summary={
                "recommended_size": proposed.recommended_size,
                "confidence_level": proposed.confidence_level,
                "cited_signals": list(proposed.cited_signals),
            },
        )

        signals = self._build_signals(
            captured=captured,
            invoked_tools=dispatcher.invoked_tools(),
            proposed=proposed,
            stated_usual_size=stated_usual_size,
            customer_id=customer_id,
            request_type=request_type,
        )

        result = validate(proposed, signals)
        if isinstance(result, RejectedDecision):
            trace.record_step(
                data_summary={"reason": result.reason},
                decision="recommendation rejected by guardrail validator",
            )
            return self._failure(
                trace, _REJECTED_MESSAGE, f"rejected: {result.reason}"
            )

        return self._present(result, proposed, trace)

    def _present(
        self,
        decision: ValidatedDecision,
        proposed: ProposedDecision,
        trace: ReasoningTraceBuilder,
    ) -> Recommendation:
        """Build the final :class:`Recommendation` from a validated decision.

        Records the validator corrections on the trace, composes the masked trace
        with the customer-facing explanation (Requirement 7.2), and flags a
        low-confidence result as advisory (Requirement 11.4). The supporting-signal
        count the validator carried is surfaced (Requirements 1.10, 4.4).
        """
        trace.record_validator_corrections(decision.corrections)
        for correction in decision.corrections:
            trace.record_step(
                data_summary={
                    "invariant": correction.invariant,
                    "proposed_value": correction.proposed_value,
                    "corrected_value": correction.corrected_value,
                },
                decision=f"validator corrected {correction.invariant}",
            )

        built = trace.build(
            rationale=decision.rationale,
            cited_signals=decision.cited_signals,
            confidence=decision.confidence,
        )

        advisory = decision.confidence == LOW
        warnings = list(decision.warnings)
        if advisory:
            warnings.append(
                "This is an advisory suggestion — confidence is low."
            )

        return Recommendation(
            ok=True,
            recommended_size=decision.size,
            confidence=decision.confidence,
            advisory=advisory,
            explanation=built.customer_facing_explanation,
            supporting_signal_count=decision.supporting_signal_count,
            warnings=warnings,
            note=decision.note,
            trace=built.full_trace(),
        )

    # -- Signals assembly from captured tool results ------------------------

    def _build_signals(
        self,
        *,
        captured: Dict[str, Any],
        invoked_tools: frozenset[str],
        proposed: ProposedDecision,
        stated_usual_size: Optional[float],
        customer_id: Optional[str],
        request_type: str,
    ) -> Signals:
        """Assemble the validator :class:`Signals` from the data Claude read.

        Reads only the captured tool results (Requirement 9.3 — no prompt-stuffed
        data). When the fit-signals tool was not successfully captured, ``oms`` is
        left ``None`` so the validator's tool-failure short-circuit applies
        (Requirement 1.2). The stated usual size falls back to the customer
        profile's ``usual_shoe_size`` when the caller did not pass one.
        """
        fit = captured.get("get_sku_fit_signals")
        catalog = captured.get("get_product_catalog_entry") or {}
        profile = captured.get("get_customer_profile")
        reviews = captured.get("get_sku_reviews") or {}
        outcomes = captured.get("get_recommendation_outcomes") or {}

        sizing_system = _sizing_system(profile)
        resolved_stated = _resolve_stated_size(stated_usual_size, profile)
        offered = _offered_sizes(catalog)

        oms_view: Optional[OmsView] = None
        if isinstance(fit, dict):
            oms_view = OmsView(
                too_big_count=_as_int(fit.get("too_big_count")),
                too_small_count=_as_int(fit.get("too_small_count")),
                uncomfortable_count=_as_int(fit.get("uncomfortable_count")),
                exchange_count=_as_int(fit.get("exchange_count")),
            )

        return Signals(
            request_type=request_type,
            invoked_tools=invoked_tools,
            stated_usual_size=resolved_stated,
            sizing_system=sizing_system,
            catalog=CatalogView(
                offered_sizes=offered,
                fit_guidance=catalog.get("fit_guidance"),
                fit_return_rate=_as_float(catalog.get("fit_return_rate")),
                toe_box=catalog.get("toe_box"),
            ),
            oms=oms_view,
            foot_length_cm=_foot_length(profile),
            customer_id_supplied=customer_id is not None,
            width=_width_signals(profile, reviews, catalog),
            outcome_signal_count=_as_int(outcomes.get("outcome_count")),
            protected_attributes_present=frozenset(),
        )

    # -- helpers ------------------------------------------------------------

    def _opening_prompt(
        self,
        *,
        sku: str,
        stated_usual_size: Optional[float],
        customer_id: Optional[str],
        direct_occasion: Optional[str],
    ) -> str:
        """Compose the opening user message (parameters only — no dataset content).

        Names the SKU, the stated usual size, the customer id (so Claude knows to
        call ``get_customer_profile``), and any directly-specified occasion. All
        actual fit data must come from the tools, so this message carries no records
        (Requirement 9.3).
        """
        lines = [f"Recommend a size for SKU {sku}."]
        if stated_usual_size is not None:
            lines.append(f"The customer's stated usual size is {stated_usual_size}.")
        if customer_id is not None:
            lines.append(
                f"A customer profile is available for customer id {customer_id}; "
                "call get_customer_profile to read it."
            )
        if direct_occasion is not None:
            lines.append(f"The customer named this occasion: {direct_occasion}.")
        lines.append(
            "Call the data tools you need, then return the structured decision."
        )
        return " ".join(lines)

    def _failure(
        self, trace: ReasoningTraceBuilder, message: str, reason: str
    ) -> Recommendation:
        """Build a failure :class:`Recommendation`, recording the reason in the trace."""
        trace.record_step(
            data_summary={"failure_reason": reason}, decision=message
        )
        built = trace.build()
        return Recommendation(
            ok=False,
            explanation=message,
            failure_reason=reason,
            trace=built.full_trace(),
        )


# ---------------------------------------------------------------------------
# Module-level helpers (pure)
# ---------------------------------------------------------------------------


def _default_dispatcher_factory(trace: ReasoningTraceBuilder) -> Any:
    """Build the real :class:`ToolDispatcher` for a run.

    Imported lazily so this module imports cleanly even if the dispatcher's own
    imports change; the factory is a seam the orchestrator constructor overrides in
    tests with a mocked dispatcher.
    """
    from ai_fit.orchestrator.dispatcher import ToolDispatcher

    return ToolDispatcher(trace)


def _backoff_seconds(attempt: int) -> float:
    """Return a short backoff delay (seconds) before the next throttled retry."""
    return 0.5 * attempt


def _output_message(response: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the assistant message from a Converse response.

    Converse returns ``{"output": {"message": {...}}}``; this returns that message
    (defaulting to an empty assistant turn so a malformed response does not crash
    the loop — it simply yields no tool uses and no final text).
    """
    output = response.get("output") or {}
    message = output.get("message")
    if isinstance(message, dict):
        return message
    return {"role": "assistant", "content": []}


def _final_text(message: Dict[str, Any]) -> str:
    """Concatenate the text blocks of a final assistant message.

    The structured decision is returned as text (JSON, possibly fenced), so the
    parser (:func:`try_parse_proposed_decision`) receives the joined text content.
    """
    parts: List[str] = []
    for block in message.get("content", []):
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return "\n".join(parts)


def _tool_result_block(
    tool_use_id: str, content: Any, *, error: bool = False
) -> Dict[str, Any]:
    """Build a Converse ``toolResult`` content block for a dispatched tool.

    The tool's JSON-serializable output is wrapped in a ``{"json": ...}`` result
    block tied to the originating ``toolUseId``; a failed call is flagged with
    ``status: "error"`` so Claude knows the data was unavailable (Requirement 7.6).
    """
    block: Dict[str, Any] = {
        "toolResult": {
            "toolUseId": tool_use_id,
            "content": [{"json": content}],
        }
    }
    if error:
        block["toolResult"]["status"] = "error"
    return block


def _sizing_system(profile: Any) -> str:
    """Return the customer's sizing system, defaulting to women's US.

    The validator's measurement lookup requires a recognized sizing system; when no
    profile was read, default to women's US so a measurement-free path still
    validates (the default never affects a non-measurement recommendation).
    """
    from ai_fit.reasoning.sizing import WOMENS_SIZING

    if isinstance(profile, dict):
        system = profile.get("sizing_system")
        if isinstance(system, str) and system.strip():
            return system
    return WOMENS_SIZING


def _resolve_stated_size(
    stated_usual_size: Optional[float], profile: Any
) -> float:
    """Resolve the stated usual size: caller value, else the profile's usual size.

    Falls back to the customer profile's ``usual_shoe_size`` when the caller did not
    pass a size, and finally to ``0.0`` when neither is available (the offered-range
    clamp then snaps any computed size onto the SKU's grid).
    """
    if stated_usual_size is not None:
        return float(stated_usual_size)
    if isinstance(profile, dict):
        value = _as_float(profile.get("usual_shoe_size"))
        if value is not None:
            return value
    return 0.0


def _offered_sizes(catalog: Any) -> List[float]:
    """Return the catalog's offered sizes as floats (empty when unavailable)."""
    if not isinstance(catalog, dict):
        return []
    sizes: List[float] = []
    for raw in catalog.get("offered_sizes") or []:
        value = _as_float(raw)
        if value is not None:
            sizes.append(value)
    return sizes


def _foot_length(profile: Any) -> Optional[float]:
    """Return the profile's foot_length_cm when present (measurement precedence)."""
    if isinstance(profile, dict):
        return _as_float(profile.get("foot_length_cm"))
    return None


def _width_signals(profile: Any, reviews: Any, catalog: Any) -> WidthSignals:
    """Assemble :class:`WidthSignals` from profile, review, and catalog tool results.

    Width/shape signals only ever raise a warning — the validator recomputes the
    warning from these and never lets them change the size (Requirements 3.9, 3.11,
    3.13).
    """
    fit_profile = profile.get("fit_profile") if isinstance(profile, dict) else None
    fit_profile = fit_profile if isinstance(fit_profile, dict) else {}

    width_counts = (
        reviews.get("width_signal_counts") if isinstance(reviews, dict) else None
    )
    width_counts = width_counts if isinstance(width_counts, dict) else {}
    narrow = _as_int(width_counts.get("narrow"))
    wide = _as_int(width_counts.get("wide"))
    normal = _as_int(width_counts.get("normal"))
    total = narrow + wide + normal
    majority: Optional[str] = None
    if total > 0:
        if narrow > wide and narrow >= normal:
            majority = "narrow"
        elif wide > narrow and wide >= normal:
            majority = "wide"
        else:
            majority = "normal"

    toe_box = catalog.get("toe_box") if isinstance(catalog, dict) else None

    return WidthSignals(
        review_width_count=total,
        review_width_majority=majority,
        customer_too_narrow=bool(fit_profile.get("too_narrow")),
        customer_too_wide=bool(fit_profile.get("too_wide")),
        customer_tight_toes=bool(fit_profile.get("tight_toes")),
        customer_heels_slip=bool(fit_profile.get("heels_slip")),
        toe_box=toe_box if isinstance(toe_box, str) else None,
    )


def _as_int(value: Any) -> int:
    """Coerce a value to ``int``, defaulting to ``0`` on ``None``/non-numeric."""
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> Optional[float]:
    """Coerce a value to ``float``, returning ``None`` on ``None``/non-numeric."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
