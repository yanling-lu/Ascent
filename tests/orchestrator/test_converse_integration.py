"""Integration tests for the Bedrock Converse loop (Task 12.3).

These exercise :class:`ai_fit.orchestrator.converse.ConverseOrchestrator` end to
end with a **mocked** :class:`BedrockConverseClient` (no live AWS) and in-memory
stub tool handlers (no real :class:`DataStore`). They cover the paths the design
"Error Handling" table and the integration-test plan call out:

* happy path — a ``tool_use`` turn whose ``toolResult`` is fed back, then a final
  structured decision that validates into a presentable
  :class:`Recommendation` with size, confidence, explanation,
  ``supporting_signal_count``, and a masked trace (Requirements 9.1, 9.3, 7.2);
* Bedrock **throttling** retried at most :data:`TOOL_MAX_RETRIES` (2) times, then a
  failure :class:`Recommendation` (Requirement 9.5);
* a Bedrock **non-throttling** error producing an immediate failure (Requirement
  9.4);
* a **validator rejection** (a mandatory tool never called) producing a
  non-presented failure (Requirement 9.7);
* an **unparseable** model decision degrading gracefully to a failure (Requirement
  9.3);
* the direct-**financial-request** decline + route path (Requirements 11.2, 11.3,
  11.4) via :func:`handle_financial_request`.

The Bedrock seam is a canned-response stub; the ``sleep`` seam is replaced with a
no-op so the throttling backoff adds no real delay.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from ai_fit.config import TOOL_MAX_RETRIES
from ai_fit.orchestrator.converse import (
    ConverseOrchestrator,
    FINANCIAL_ACTION_TYPES,
    Recommendation,
    ThrottlingError,
    ToolSpec,
    handle_financial_request,
)
from ai_fit.reasoning.validator import REQUEST_SIZE
from ai_fit.tools.human_routing import AuditLog
from ai_fit.trace.builder import MASK_PLACEHOLDER


# ---------------------------------------------------------------------------
# Canned Converse responses (mirrors the bedrock-runtime converse shape)
# ---------------------------------------------------------------------------


def _tool_use_response(tool_calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    """A Converse response asking the agent to run one or more tools."""
    content = [
        {
            "toolUse": {
                "toolUseId": call["id"],
                "name": call["name"],
                "input": call.get("input", {}),
            }
        }
        for call in tool_calls
    ]
    return {
        "stopReason": "tool_use",
        "output": {"message": {"role": "assistant", "content": content}},
    }


def _final_response(decision: Any) -> Dict[str, Any]:
    """A Converse response with the final structured decision as JSON text."""
    text = decision if isinstance(decision, str) else json.dumps(decision)
    return {
        "stopReason": "end_turn",
        "output": {
            "role": "assistant",
            "message": {"role": "assistant", "content": [{"text": text}]},
        },
    }


class ScriptedBedrockClient:
    """A :class:`BedrockConverseClient` that replays a canned script of responses.

    Each ``converse`` call pops the next scripted item: either a response dict to
    return, or an exception instance to raise (used to drive the throttling /
    non-throttling error paths). Every call's arguments are recorded so the tests
    can assert how many Converse invocations happened and what was sent.
    """

    def __init__(self, script: List[Any]) -> None:
        self._script = list(script)
        self.calls: List[Dict[str, Any]] = []

    def converse(
        self,
        *,
        model_id: str,
        messages: List[Dict[str, Any]],
        system: List[Dict[str, Any]],
        tool_config: Dict[str, Any],
        temperature: float,
    ) -> Dict[str, Any]:
        self.calls.append(
            {
                "model_id": model_id,
                "messages": [dict(m) for m in messages],
                "temperature": temperature,
                "tool_config": tool_config,
            }
        )
        if not self._script:
            raise AssertionError("ScriptedBedrockClient ran out of scripted responses")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


# ---------------------------------------------------------------------------
# Stub tool handlers (small canned dicts — no real DataStore)
# ---------------------------------------------------------------------------


def _canned_tool_specs() -> List[ToolSpec]:
    """The four size-recommendation tools with in-memory canned handlers.

    The canned data drives a clean size path: OMS fit history leans *too small*
    (so a size-up is justified) with >= 5 signals (not sparse, so confidence may be
    high), offered sizes include the recommended 8.0, and the customer profile
    carries the two direct identifiers so the trace-masking assertion has something
    to redact.
    """
    schema = {"type": "object", "properties": {"sku": {"type": "string"}}}

    def fit_signals(**_: Any) -> Dict[str, Any]:
        return {
            "too_big_count": 1,
            "too_small_count": 12,
            "uncomfortable_count": 1,
            "exchange_count": 3,
        }

    def catalog(**_: Any) -> Dict[str, Any]:
        return {
            "found": True,
            "offered_sizes": [6.0, 6.5, 7.0, 7.5, 8.0, 8.5],
            "fit_guidance": "runs small",
            "fit_return_rate": 0.08,
            "toe_box": "regular",
        }

    def reviews(**_: Any) -> Dict[str, Any]:
        return {"width_signal_counts": {"narrow": 0, "wide": 0, "normal": 4}}

    def profile(**_: Any) -> Dict[str, Any]:
        return {
            "sizing_system": "Women's sizing",
            "usual_shoe_size": 7.5,
            # Direct identifiers that MUST be masked out of the trace (Req 7.5/10.3).
            "customer_uid": "CUST-12345",
            "cnsmr_prof_sid": "SID-98765",
            "fit_profile": {},
        }

    return [
        ToolSpec("get_sku_fit_signals", "fit history", schema, fit_signals),
        ToolSpec("get_product_catalog_entry", "catalog", schema, catalog),
        ToolSpec("get_sku_reviews", "reviews", schema, reviews),
        ToolSpec("get_customer_profile", "profile", schema, profile),
    ]


def _orchestrator(client: ScriptedBedrockClient, tool_specs=None) -> ConverseOrchestrator:
    """Build an orchestrator wired to the mock client with a no-op backoff sleep."""
    return ConverseOrchestrator(
        client,
        tool_specs if tool_specs is not None else _canned_tool_specs(),
        sleep=lambda _seconds: None,  # no real backoff delay in tests
    )


_HAPPY_DECISION = {
    "recommended_size": "8.0",
    "confidence_level": "high",
    "size_rationale": "Past returns for this style skew toward too small, so size up.",
    "cited_signals": ["oms_fit_returns", "oms_exchanges"],
}


# ---------------------------------------------------------------------------
# Happy path (Requirements 9.1, 9.3, 7.2)
# ---------------------------------------------------------------------------


def test_happy_path_tool_use_then_final_decision_validates():
    """tool_use turn -> toolResult fed back -> final decision -> presented Recommendation."""
    client = ScriptedBedrockClient(
        [
            _tool_use_response(
                [
                    {"id": "t1", "name": "get_sku_fit_signals", "input": {"sku": "SKU1"}},
                    {"id": "t2", "name": "get_product_catalog_entry", "input": {"sku": "SKU1"}},
                    {"id": "t3", "name": "get_sku_reviews", "input": {"sku": "SKU1"}},
                    {"id": "t4", "name": "get_customer_profile", "input": {"sku": "SKU1"}},
                ]
            ),
            _final_response(_HAPPY_DECISION),
        ]
    )
    orch = _orchestrator(client)

    rec = orch.produce_recommendation(
        "SKU1", stated_usual_size=7.5, customer_id="CUST-12345", request_type=REQUEST_SIZE
    )

    assert isinstance(rec, Recommendation)
    assert rec.ok is True
    assert rec.recommended_size == 8.0
    assert rec.confidence == "high"
    # Explanation cites a historical signal and is plain language (Req 7.2).
    assert rec.explanation
    assert "fit-related returns" in rec.explanation or "exchanges" in rec.explanation
    # Supporting-signal count includes OMS fit + exchange signals (1+12+1+3 = 17).
    assert rec.supporting_signal_count == 17
    # Two Converse calls: the tool-use turn and the final decision turn.
    assert len(client.calls) == 2
    # Temperature 0 was used (Req 9.1).
    assert client.calls[0]["temperature"] == 0.0


def test_happy_path_feeds_tool_results_back_as_user_turn():
    """The loop sends a second Converse call carrying the toolResult user message."""
    client = ScriptedBedrockClient(
        [
            _tool_use_response(
                [
                    {"id": "t1", "name": "get_sku_fit_signals", "input": {"sku": "SKU1"}},
                    {"id": "t2", "name": "get_product_catalog_entry", "input": {"sku": "SKU1"}},
                    {"id": "t3", "name": "get_sku_reviews", "input": {"sku": "SKU1"}},
                    {"id": "t4", "name": "get_customer_profile", "input": {"sku": "SKU1"}},
                ]
            ),
            _final_response(_HAPPY_DECISION),
        ]
    )
    orch = _orchestrator(client)

    orch.produce_recommendation(
        "SKU1", stated_usual_size=7.5, customer_id="CUST-12345"
    )

    # The second Converse call's message history must include a toolResult block.
    second_call_messages = client.calls[1]["messages"]
    tool_result_present = any(
        isinstance(block, dict) and "toolResult" in block
        for message in second_call_messages
        for block in (message.get("content") or [])
    )
    assert tool_result_present


def test_happy_path_trace_masks_direct_identifiers():
    """The returned masked trace never leaks customer_uid / cnsmr_prof_sid values."""
    client = ScriptedBedrockClient(
        [
            _tool_use_response(
                [
                    {"id": "t1", "name": "get_sku_fit_signals", "input": {"sku": "SKU1"}},
                    {"id": "t2", "name": "get_product_catalog_entry", "input": {"sku": "SKU1"}},
                    {"id": "t3", "name": "get_sku_reviews", "input": {"sku": "SKU1"}},
                    {"id": "t4", "name": "get_customer_profile", "input": {"sku": "SKU1"}},
                ]
            ),
            _final_response(_HAPPY_DECISION),
        ]
    )
    orch = _orchestrator(client)

    rec = orch.produce_recommendation(
        "SKU1", stated_usual_size=7.5, customer_id="CUST-12345"
    )

    assert rec.trace is not None
    blob = json.dumps(rec.trace)
    # The raw identifier values from the profile tool must not appear anywhere.
    assert "CUST-12345" not in blob
    assert "SID-98765" not in blob
    # The redaction placeholder is present where the profile was recorded.
    assert MASK_PLACEHOLDER in blob
    assert "customer_uid" in rec.trace["masked_fields"]


# ---------------------------------------------------------------------------
# Throttling: retry at most TOOL_MAX_RETRIES then fail (Requirement 9.5)
# ---------------------------------------------------------------------------


def test_throttling_retried_then_fails_after_max_retries():
    """Throttling on every attempt -> fail after 1 + TOOL_MAX_RETRIES attempts."""
    # One more throttle than the retry budget guarantees the loop exhausts retries.
    script = [ThrottlingError("slow down")] * (TOOL_MAX_RETRIES + 1)
    client = ScriptedBedrockClient(script)
    orch = _orchestrator(client)

    rec = orch.produce_recommendation("SKU1", stated_usual_size=7.5)

    assert rec.ok is False
    assert rec.failure_reason == "bedrock_throttled"
    assert rec.recommended_size is None
    # Exactly 1 initial attempt + TOOL_MAX_RETRIES retries were made, no more.
    assert len(client.calls) == TOOL_MAX_RETRIES + 1


def test_throttling_recovers_within_retry_budget():
    """A throttle that clears within budget still produces a recommendation."""
    client = ScriptedBedrockClient(
        [
            ThrottlingError("slow down"),  # first attempt throttled
            _tool_use_response(
                [
                    {"id": "t1", "name": "get_sku_fit_signals", "input": {"sku": "SKU1"}},
                    {"id": "t2", "name": "get_product_catalog_entry", "input": {"sku": "SKU1"}},
                    {"id": "t3", "name": "get_sku_reviews", "input": {"sku": "SKU1"}},
                ]
            ),
            _final_response(_HAPPY_DECISION),
        ]
    )
    orch = _orchestrator(client)

    rec = orch.produce_recommendation("SKU1", stated_usual_size=7.5)

    assert rec.ok is True
    assert rec.recommended_size == 8.0


# ---------------------------------------------------------------------------
# Non-throttling Bedrock error -> immediate fail (Requirement 9.4)
# ---------------------------------------------------------------------------


def test_non_throttling_error_fails_immediately():
    """A non-throttling Bedrock error fails on the first attempt, with no retry."""
    client = ScriptedBedrockClient([RuntimeError("model unavailable")])
    orch = _orchestrator(client)

    rec = orch.produce_recommendation("SKU1", stated_usual_size=7.5)

    assert rec.ok is False
    assert rec.failure_reason is not None
    assert rec.failure_reason.startswith("bedrock_error")
    # No retry on a non-throttling error: exactly one Converse call.
    assert len(client.calls) == 1


# ---------------------------------------------------------------------------
# Validator rejection: missing required tool -> non-presented failure (Req 9.7)
# ---------------------------------------------------------------------------


def test_missing_required_tool_rejected_and_not_presented():
    """A size decision whose mandatory tools were not all called is rejected."""
    # Claude calls only the catalog tool, skipping fit signals and reviews, then
    # returns a decision. The required-tool guard must reject it.
    client = ScriptedBedrockClient(
        [
            _tool_use_response(
                [{"id": "t2", "name": "get_product_catalog_entry", "input": {"sku": "SKU1"}}]
            ),
            _final_response(_HAPPY_DECISION),
        ]
    )
    orch = _orchestrator(client)

    rec = orch.produce_recommendation(
        "SKU1", stated_usual_size=7.5, request_type=REQUEST_SIZE
    )

    assert rec.ok is False
    assert rec.failure_reason is not None
    assert rec.failure_reason.startswith("rejected")
    assert "required tool" in rec.failure_reason
    # Nothing presentable leaked through.
    assert rec.recommended_size is None


# ---------------------------------------------------------------------------
# Unparseable model decision -> graceful failure (Requirement 9.3)
# ---------------------------------------------------------------------------


def test_unparseable_decision_degrades_gracefully():
    """A final message with no parseable decision yields a failure, not a crash."""
    client = ScriptedBedrockClient(
        [
            _tool_use_response(
                [
                    {"id": "t1", "name": "get_sku_fit_signals", "input": {"sku": "SKU1"}},
                    {"id": "t2", "name": "get_product_catalog_entry", "input": {"sku": "SKU1"}},
                    {"id": "t3", "name": "get_sku_reviews", "input": {"sku": "SKU1"}},
                ]
            ),
            _final_response("I was unable to produce a structured decision."),
        ]
    )
    orch = _orchestrator(client)

    rec = orch.produce_recommendation("SKU1", stated_usual_size=7.5)

    assert rec.ok is False
    assert rec.failure_reason == "unparseable_decision"
    assert rec.recommended_size is None
    # The loop still produced a (masked) trace for the reviewer.
    assert rec.trace is not None


# ---------------------------------------------------------------------------
# Direct financial request: decline + route (Requirements 11.2, 11.3, 11.4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action_type", sorted(FINANCIAL_ACTION_TYPES))
def test_financial_request_declined_and_routed(action_type):
    """Every financial action type is declined, routed, and audited (no model call)."""
    audit_log = AuditLog()

    result = handle_financial_request(
        request="Please refund my order 123.",
        action_type=action_type,
        audit_log=audit_log,
    )

    assert result.declined is True
    assert result.routed is True
    assert result.action_type == action_type
    assert result.audit_id  # a non-empty audit id was issued
    assert result.timestamp
    assert "human reviewer" in result.message.lower()
    # Exactly one audit entry recording the triggering request + routed action.
    assert len(audit_log) == 1
    entry = audit_log.entries[0]
    assert entry.triggering_request == "Please refund my order 123."
    assert entry.routed_action_type == action_type


def test_financial_request_unknown_action_normalized_to_order_modification():
    """An unrecognized action type is still declined + routed, normalized safely."""
    audit_log = AuditLog()

    result = handle_financial_request(
        request="cancel and re-ship", action_type="totally_unknown", audit_log=audit_log
    )

    assert result.declined is True
    assert result.routed is True
    assert result.action_type == "order_modification"
    assert len(audit_log) == 1
