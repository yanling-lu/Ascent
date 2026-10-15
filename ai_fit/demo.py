"""Demo entry point for the AI Fit agent (Task 13.1).

Drives a representative SKU + customer through the **full orchestrator path** and
surfaces the four things the demo must show (design "Request lifecycle"; Requirements
7.1-7.4):

* the customer-facing **explanation** citing >= 1 historical signal (Requirement 7.2),
* the **confidence** level (Requirement 7.3),
* the **occasion match** result (Requirement 2 / design "Occasion Normalization"),
* the masked **Reasoning_Trace** — with ``customer_uid`` / ``cnsmr_prof_sid`` redacted
  (Requirements 7.1, 7.4, 7.5, 10.3).

How it wires the existing modules (no new behavior is implemented here — the demo is
pure composition):

* :func:`ai_fit.data.store.DataStore.load` builds the in-memory store from the six
  CSVs (:data:`ai_fit.config.DATASET_PATHS`).
* :class:`ai_fit.tools.outcomes.OutcomeStore` + an adapter satisfy the
  ``get_recommendation_outcomes`` reader protocol.
* The 10 Agent_Tools in ``ai_fit/tools/*`` are bound to the loaded store / stores and
  exposed to Claude as :class:`ai_fit.orchestrator.ToolSpec` entries, their input
  schemas drawn from the design's tool catalog.
* :class:`ai_fit.orchestrator.ConverseOrchestrator` runs the Bedrock Converse tool-use
  loop at temperature 0, validates the decision through the Guardrail Validator, and
  returns a :class:`ai_fit.orchestrator.Recommendation` with the masked trace.
* :func:`ai_fit.tools.alternatives.match_occasion` produces the occasion-match result.

Robustness: this calls real Bedrock by default, but the Bedrock client is **injectable**
(``make_orchestrator(..., client=...)``). When credentials / model access are absent,
pass :func:`build_stub_client` (or run with ``AI_FIT_DEMO_STUB=1`` / ``--stub``) and the
demo runs entirely offline against a canned, rubric-consistent Converse response —
exercising the same loop, validator, trace masking, and occasion match end-to-end.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ai_fit import config
from ai_fit.data.store import DataStore
from ai_fit.orchestrator import (
    BedrockConverseClient,
    BotoBedrockConverseClient,
    ConverseOrchestrator,
    Recommendation,
    ToolSpec,
)
from ai_fit.tools.alternatives import find_alternative_styles, match_occasion
from ai_fit.tools.outcomes import OutcomeStore
from ai_fit.tools.quality_report import generate_quality_issue_report
from ai_fit.tools.read_core import (
    get_customer_profile,
    get_product_catalog_entry,
    get_sku_fit_signals,
)
from ai_fit.tools.read_signals import (
    get_quality_returns_and_units,
    get_recommendation_outcomes,
    get_sku_reviews,
)

# ---------------------------------------------------------------------------
# Representative SKU + customer (verified present in the datasets)
# ---------------------------------------------------------------------------

# SKU 15887957 carries a strong OMS fit history (102 fit+exchange signals leaning
# clearly "too small" -> size up), a "runs small" catalog fit_guidance that agrees
# with that lean, and work / everyday / special-occasion suitability — a clean,
# high-confidence representative case.
DEMO_SKU: str = "15887957"

# ALDO_SYN_0005: Women's sizing, usual size 7.5, shops for "work" (which the SKU
# suits -> a positive occasion match), and reports shoes often run too small
# (a tendency that reinforces the OMS size-up). No foot measurement, so the OMS
# path drives the size rather than measurement precedence.
DEMO_CUSTOMER_ID: str = "ALDO_SYN_0005"

# The stated usual size the customer is shopping at (women's US).
DEMO_STATED_SIZE: float = 7.5

# The occasion the customer named for this visit.
DEMO_OCCASION: str = "work"


# ---------------------------------------------------------------------------
# Outcome reader adapter (bridges OutcomeStore -> get_recommendation_outcomes)
# ---------------------------------------------------------------------------


class _OutcomeStoreReader:
    """Adapt an :class:`OutcomeStore` to the ``OutcomeReader`` protocol.

    ``get_recommendation_outcomes`` reads resolved per-SKU outcome counts through a
    minimal reader protocol; the real write store exposes records rather than
    aggregated counts, so this adapter tallies the terminal statuses (excluding the
    initial ``unknown`` state) into the ``{kept, returned_too_big,
    returned_too_small, returned_other}`` shape the tool expects.
    """

    def __init__(self, store: OutcomeStore) -> None:
        self._store = store

    def outcome_counts_for_sku(self, sku: str) -> Mapping[str, int]:
        counts = {
            "kept": 0,
            "returned_too_big": 0,
            "returned_too_small": 0,
            "returned_other": 0,
        }
        for outcome in self._store.get_outcomes_for_sku(sku):
            status = outcome.status
            if status == "kept":
                counts["kept"] += 1
            elif status == "returned too big":
                counts["returned_too_big"] += 1
            elif status == "returned too small":
                counts["returned_too_small"] += 1
            elif status == "returned other":
                counts["returned_other"] += 1
        return counts


# ---------------------------------------------------------------------------
# Tool specs — bind each Agent_Tool handler to the loaded store / stores
# ---------------------------------------------------------------------------

_SKU_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {"sku": {"type": "string", "description": "The product SKU."}},
    "required": ["sku"],
}
_CUSTOMER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "synthetic_customer_id": {
            "type": "string",
            "description": "The synthetic customer id to read the fit profile for.",
        }
    },
    "required": ["synthetic_customer_id"],
}
_NO_ARG_SCHEMA: Dict[str, Any] = {"type": "object", "properties": {}}
_OUTCOME_WRITE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "sku": {"type": "string"},
        "recommended_size": {"type": "string"},
        "order_number": {"type": "string"},
    },
    "required": ["sku", "recommended_size", "order_number"],
}
_ROUTE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "triggering_request": {"type": "string"},
        "action_type": {"type": "string"},
    },
    "required": ["triggering_request", "action_type"],
}
_ALTERNATIVES_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "requested_occasion": {"type": "string"},
        "browsed_sku": {"type": "string"},
    },
    "required": ["requested_occasion", "browsed_sku"],
}


def build_tool_specs(
    store: DataStore,
    outcome_store: OutcomeStore,
    audit_log: Any,
) -> List[ToolSpec]:
    """Build the 10 Agent_Tool specs, each bound to the loaded store / stores.

    Every data source is reached through exactly one dedicated tool (Requirement
    9.2). Each handler closes over the loaded :class:`DataStore` (and the write
    stores) so Claude invokes it with only the model-supplied input arguments; the
    dispatcher forwards those as keyword args.
    """
    from ai_fit.tools.human_routing import route_to_human_reviewer
    from ai_fit.tools.outcomes import record_recommendation_outcome

    outcome_reader = _OutcomeStoreReader(outcome_store)

    return [
        ToolSpec(
            name="get_sku_fit_signals",
            description="OMS fit-return and exchange signals for a SKU.",
            input_schema=_SKU_SCHEMA,
            handler=lambda sku: get_sku_fit_signals(store, sku),
        ),
        ToolSpec(
            name="get_customer_profile",
            description="The customer's fit profile (protected attributes excluded).",
            input_schema=_CUSTOMER_SCHEMA,
            handler=lambda synthetic_customer_id: get_customer_profile(
                store, synthetic_customer_id
            ),
        ),
        ToolSpec(
            name="get_product_catalog_entry",
            description="Catalog attributes for a SKU (guidance, return rate, sizes).",
            input_schema=_SKU_SCHEMA,
            handler=lambda sku: get_product_catalog_entry(store, sku),
        ),
        ToolSpec(
            name="get_sku_reviews",
            description="Review-derived fit/width/occasion signals for a SKU.",
            input_schema=_SKU_SCHEMA,
            handler=lambda sku: get_sku_reviews(store, sku),
        ),
        ToolSpec(
            name="get_recommendation_outcomes",
            description="Learned kept/returned outcome signals for a SKU.",
            input_schema=_SKU_SCHEMA,
            handler=lambda sku: get_recommendation_outcomes(outcome_reader, sku),
        ),
        ToolSpec(
            name="get_quality_returns_and_units",
            description="Per-SKU quality returns and units sold (report input).",
            input_schema=_NO_ARG_SCHEMA,
            handler=lambda: get_quality_returns_and_units(store),
        ),
        ToolSpec(
            name="record_recommendation_outcome",
            description="Write a new recommendation outcome (initial 'unknown').",
            input_schema=_OUTCOME_WRITE_SCHEMA,
            handler=lambda sku, recommended_size, order_number: record_recommendation_outcome(
                outcome_store, sku, recommended_size, order_number
            ),
        ),
        ToolSpec(
            name="generate_quality_issue_report",
            description="Compute and return the ranked product-quality report.",
            input_schema=_NO_ARG_SCHEMA,
            handler=lambda: generate_quality_issue_report(store),
        ),
        ToolSpec(
            name="route_to_human_reviewer",
            description="Route a financial/order action to a human reviewer (intent only).",
            input_schema=_ROUTE_SCHEMA,
            handler=lambda triggering_request, action_type: route_to_human_reviewer(
                triggering_request, action_type, audit_log
            ),
        ),
        ToolSpec(
            name="find_alternative_styles",
            description="Occasion-matching alternative styles for a browsed SKU.",
            input_schema=_ALTERNATIVES_SCHEMA,
            handler=lambda requested_occasion, browsed_sku: find_alternative_styles(
                store, requested_occasion, browsed_sku
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# Orchestrator wiring
# ---------------------------------------------------------------------------


@dataclass
class DemoAgent:
    """The wired-up demo agent: a store, the orchestrator, and the tool specs.

    Bundles the loaded :class:`DataStore` (reused for the direct occasion match) with
    the :class:`ConverseOrchestrator` so the demo can drive a size recommendation and
    an occasion assessment against the same in-memory data.
    """

    store: DataStore
    orchestrator: ConverseOrchestrator
    tool_specs: List[ToolSpec]


def make_orchestrator(
    client: Optional[BedrockConverseClient] = None,
    *,
    dataset_paths: Optional[Mapping[str, Any]] = None,
) -> DemoAgent:
    """Load the data and wire the orchestrator with the 10 bound tool specs.

    ``client`` is the injectable Bedrock seam: pass a real
    :class:`~ai_fit.orchestrator.BotoBedrockConverseClient` (the default when
    ``None``) for live Bedrock, or :func:`build_stub_client` to run offline. The
    datasets are loaded once from :data:`ai_fit.config.DATASET_PATHS` (overridable
    via ``dataset_paths`` for tests).
    """
    from ai_fit.tools.human_routing import AuditLog

    store = DataStore.load(dataset_paths)
    outcome_store = OutcomeStore()
    audit_log = AuditLog()
    tool_specs = build_tool_specs(store, outcome_store, audit_log)

    if client is None:
        client = BotoBedrockConverseClient()

    orchestrator = ConverseOrchestrator(client, tool_specs)
    return DemoAgent(store=store, orchestrator=orchestrator, tool_specs=tool_specs)


# ---------------------------------------------------------------------------
# Stub Bedrock client — runs the whole loop offline (no AWS required)
# ---------------------------------------------------------------------------


def build_stub_client(
    *,
    recommended_size: str,
    confidence_level: str,
    size_rationale: str,
    cited_signals: Sequence[str],
    width_warning: Optional[str] = None,
    comfort_note: Optional[str] = None,
) -> BedrockConverseClient:
    """Build a canned :class:`BedrockConverseClient` that drives the full loop offline.

    The stub mimics genuine agentic tool use so the demo exercises the real path with
    no live Bedrock:

    1. On the first turn it emits a ``tool_use`` response requesting every tool the
       required-tool guard needs for a size recommendation
       (``get_sku_fit_signals``, ``get_product_catalog_entry``, ``get_sku_reviews``,
       ``get_customer_profile``) plus ``get_recommendation_outcomes``. The
       orchestrator dispatches them against the real :class:`DataStore` and feeds the
       results back.
    2. On the next turn (now that tool results are present in the message history) it
       returns the final structured decision as JSON text, exactly matching the
       Structured Output Contract, so the parser + Guardrail Validator + trace builder
       all run for real.

    The decision values are supplied by the caller so they stay consistent with the
    rubric for the chosen SKU; the validator still independently clamps/validates them.
    """

    decision: Dict[str, Any] = {
        "recommended_size": recommended_size,
        "confidence_level": confidence_level,
        "size_rationale": size_rationale,
        "cited_signals": list(cited_signals),
    }
    if width_warning:
        decision["width_warning"] = width_warning
    if comfort_note:
        decision["comfort_note"] = comfort_note

    return _StubConverseClient(decision)


class _StubConverseClient:
    """Internal canned Converse client (see :func:`build_stub_client`)."""

    # The SKU-keyed tools the stub asks for first (and the single customer tool).
    _SKU_TOOLS = (
        "get_sku_fit_signals",
        "get_product_catalog_entry",
        "get_sku_reviews",
        "get_recommendation_outcomes",
    )

    def __init__(self, decision: Dict[str, Any]) -> None:
        self._decision = decision

    def converse(
        self,
        *,
        model_id: str,
        messages: List[Dict[str, Any]],
        system: List[Dict[str, Any]],
        tool_config: Dict[str, Any],
        temperature: float,
    ) -> Dict[str, Any]:
        """Return a tool-use request, then the final decision once tools have run."""
        if not _has_tool_results(messages):
            return self._tool_use_response(messages)
        return self._final_decision_response()

    def _tool_use_response(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Emit a ``tool_use`` turn requesting the required data tools."""
        sku, customer_id = _extract_demo_params(messages)
        content: List[Dict[str, Any]] = []
        for name in self._SKU_TOOLS:
            content.append(
                {
                    "toolUse": {
                        "toolUseId": f"stub-{name}",
                        "name": name,
                        "input": {"sku": sku},
                    }
                }
            )
        if customer_id is not None:
            content.append(
                {
                    "toolUse": {
                        "toolUseId": "stub-get_customer_profile",
                        "name": "get_customer_profile",
                        "input": {"synthetic_customer_id": customer_id},
                    }
                }
            )
        return {
            "stopReason": "tool_use",
            "output": {"message": {"role": "assistant", "content": content}},
        }

    def _final_decision_response(self) -> Dict[str, Any]:
        """Emit the final structured decision as JSON text (end_turn)."""
        return {
            "stopReason": "end_turn",
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [{"text": json.dumps(self._decision)}],
                }
            },
        }


def _has_tool_results(messages: Sequence[Mapping[str, Any]]) -> bool:
    """Return True if any message already carries a ``toolResult`` block."""
    for message in messages:
        for block in message.get("content", []) or []:
            if isinstance(block, Mapping) and "toolResult" in block:
                return True
    return False


def _extract_demo_params(
    messages: Sequence[Mapping[str, Any]],
) -> tuple[str, Optional[str]]:
    """Best-effort extract the SKU and customer id from the opening user prompt.

    The orchestrator's opening prompt names the SKU as ``SKU <sku>`` and, when a
    customer id was supplied, ``customer id <id>``. The stub parses them back out so
    it requests tools for the right SKU/customer. Falls back to the demo constants if
    parsing finds nothing (the demo always drives those).
    """
    text = ""
    for message in messages:
        for block in message.get("content", []) or []:
            if isinstance(block, Mapping) and isinstance(block.get("text"), str):
                text += " " + block["text"]

    sku = DEMO_SKU
    customer_id: Optional[str] = None
    tokens = text.replace(".", " ").replace(";", " ").split()
    for i, tok in enumerate(tokens):
        if tok == "SKU" and i + 1 < len(tokens):
            sku = tokens[i + 1]
        if tok == "id" and i + 1 < len(tokens) and tokens[i + 1].startswith("ALDO_"):
            customer_id = tokens[i + 1]
    return sku, customer_id


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _render_recommendation(rec: Recommendation) -> List[str]:
    """Render the size recommendation block (explanation, confidence, warnings)."""
    lines: List[str] = []
    lines.append("SIZE RECOMMENDATION")
    lines.append("-" * 60)
    if not rec.ok:
        lines.append(f"  No recommendation produced: {rec.failure_reason}")
        lines.append(f"  Message: {rec.explanation}")
        return lines

    lines.append(f"  Recommended size : {rec.recommended_size}")
    lines.append(f"  Confidence       : {rec.confidence}"
                 + ("  (advisory)" if rec.advisory else ""))
    lines.append(f"  Supporting signals: {rec.supporting_signal_count}")
    lines.append("")
    lines.append("  Customer-facing explanation (Req 7.2):")
    lines.append(f"    {rec.explanation}")
    if rec.warnings:
        lines.append("")
        lines.append("  Warnings:")
        for warning in rec.warnings:
            lines.append(f"    - {warning}")
    if rec.note:
        lines.append(f"  Note: {rec.note}")
    return lines


def _render_occasion(match: Mapping[str, Any]) -> List[str]:
    """Render the occasion-match block."""
    lines: List[str] = []
    lines.append("OCCASION MATCH")
    lines.append("-" * 60)
    lines.append(f"  Status            : {match.get('status')}")
    lines.append(f"  Customer occasions: {match.get('customer_occasions')}")
    lines.append(f"  SKU occasions     : {match.get('sku_occasions')}")
    lines.append(f"  Matched occasions : {match.get('matched_occasions')}")
    lines.append(f"  Message           : {match.get('message')}")
    return lines


def _render_trace(trace: Optional[Mapping[str, Any]]) -> List[str]:
    """Render the masked Reasoning_Trace block (Req 7.1, 7.4, 7.5, 10.3)."""
    lines: List[str] = []
    lines.append("MASKED REASONING TRACE (Req 7.1, 7.4, 7.5, 10.3)")
    lines.append("-" * 60)
    if not trace:
        lines.append("  (no trace)")
        return lines

    lines.append(f"  Masked fields: {trace.get('masked_fields')}")
    lines.append("  Steps (in execution order):")
    for step in trace.get("steps", []):
        order = step.get("order")
        step_type = step.get("step_type")
        tool_name = step.get("tool_name")
        decision = step.get("decision")
        head = f"    {order:>2}. [{step_type}]"
        if tool_name:
            head += f" {tool_name}"
        lines.append(head)
        if decision:
            lines.append(f"        -> {decision}")

    corrections = trace.get("validator_corrections", [])
    if corrections:
        lines.append("  Validator corrections:")
        for c in corrections:
            lines.append(
                f"    - {c.get('invariant')}: "
                f"{c.get('proposed_value')} -> {c.get('corrected_value')}"
            )
    failed = trace.get("failed_tools", [])
    if failed:
        lines.append("  Failed tools:")
        for f in failed:
            lines.append(f"    - {f.get('tool_name')}: {f.get('unavailable_data')}")
    return lines


def render_report(rec: Recommendation, occasion_match: Mapping[str, Any]) -> str:
    """Assemble the full, readable demo report string."""
    lines: List[str] = []
    lines.append("=" * 60)
    lines.append("AI FIT — DEMO")
    lines.append("=" * 60)
    lines.append(f"  SKU         : {DEMO_SKU}")
    lines.append(f"  Customer    : {DEMO_CUSTOMER_ID}")
    lines.append(f"  Stated size : {DEMO_STATED_SIZE}")
    lines.append(f"  Occasion    : {DEMO_OCCASION}")
    lines.append("")
    lines.extend(_render_recommendation(rec))
    lines.append("")
    lines.extend(_render_occasion(occasion_match))
    lines.append("")
    lines.extend(_render_trace(rec.trace))
    lines.append("=" * 60)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Demo driver
# ---------------------------------------------------------------------------


def _default_stub() -> BedrockConverseClient:
    """The stub decision for the demo SKU/customer, consistent with the rubric.

    SKU 15887957 leans clearly "too small" over 102 OMS signals with agreeing
    catalog guidance, and the customer reports shoes often run too small — so the
    rubric points one half size up from 7.5 to 8.0 at high confidence, citing the OMS
    returns, the customer's saved fit profile, and the catalog guidance.
    """
    return build_stub_client(
        recommended_size="8.0",
        confidence_level="high",
        size_rationale=(
            "This style has a strong history of running small — most fit-related "
            "returns were because it felt too small — and the product's own fit "
            "guidance says it runs small, so a half size up from your usual 7.5 "
            "should fit better."
        ),
        cited_signals=["oms_fit_returns", "customer_fit_profile", "catalog_fit_guidance"],
    )


def run_demo(client: Optional[BedrockConverseClient] = None) -> Recommendation:
    """Run the full demo path and print the report; return the recommendation.

    Drives the representative SKU + customer through the orchestrator
    (:meth:`ConverseOrchestrator.produce_recommendation`) for the size recommendation,
    and :func:`ai_fit.tools.alternatives.match_occasion` for the occasion result, then
    prints a readable report covering the explanation, confidence, occasion match, and
    masked trace. ``client`` is the injectable Bedrock seam.
    """
    agent = make_orchestrator(client)

    rec = agent.orchestrator.produce_recommendation(
        DEMO_SKU,
        stated_usual_size=DEMO_STATED_SIZE,
        customer_id=DEMO_CUSTOMER_ID,
    )

    occasion_match = match_occasion(
        agent.store,
        DEMO_SKU,
        direct_occasions=[DEMO_OCCASION],
    )

    print(render_report(rec, occasion_match))
    return rec


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI Fit demo entry point.")
    parser.add_argument(
        "--stub",
        action="store_true",
        help="Run offline with a canned Bedrock response (no AWS required).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point.

    By default uses the real Bedrock client; pass ``--stub`` (or set
    ``AI_FIT_DEMO_STUB=1``) to run entirely offline. On a Bedrock failure the demo
    still prints the (failure) report and the masked trace rather than crashing.
    """
    args = _parse_args(argv)
    use_stub = args.stub or os.environ.get("AI_FIT_DEMO_STUB") == "1"

    missing = config.missing_datasets()
    if missing:
        print(
            "Cannot run demo — missing dataset CSV(s): " + ", ".join(sorted(missing)),
            file=sys.stderr,
        )
        return 2

    client: Optional[BedrockConverseClient] = _default_stub() if use_stub else None
    rec = run_demo(client)
    return 0 if rec.ok else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
