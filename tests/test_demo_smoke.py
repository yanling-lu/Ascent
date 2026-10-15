"""Smoke tests for model wiring and the demo scenario (Task 13.2).

Covers the two things the task calls for (Requirements 9.1, 7.1-7.4):

* **Config / model wiring** — the agent is wired to a Claude Opus model id on the
  Bedrock runtime at temperature 0 with the 10s / 2-retry tool-dispatch budget, and
  :func:`ai_fit.demo.build_tool_specs` produces exactly the 10 Agent_Tools with the
  right names and input schemas (Requirement 9.1).
* **End-to-end demo scenario** — the full orchestrator path runs with the injected
  **stub** Bedrock client (``build_stub_client`` / ``_default_stub``, no live AWS)
  and produces a size recommendation, an occasion-match result, and a masked
  Reasoning_Trace with ``masked_fields == [customer_uid, cnsmr_prof_sid]``; and
  :func:`ai_fit.demo.render_report` renders a string covering the explanation,
  confidence, occasion, and trace (Requirements 7.1, 7.2, 7.3, 7.4).

Performance / hermeticity: ``run_demo`` / ``make_orchestrator`` call
``DataStore.load`` against the ~1M-row production CSVs (~90s). To keep this smoke
test fast and fully offline, the end-to-end test builds a **tiny in-memory
DataStore** (a handful of raw rows for the demo SKU + customer) and wires a
``ConverseOrchestrator`` with ``build_tool_specs`` + the stub client directly —
mirroring ``run_demo``'s wiring but against the small store. No test here loads the
real datasets.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from ai_fit import config, demo
from ai_fit.data.store import DataStore
from ai_fit.orchestrator import ConverseOrchestrator
from ai_fit.tools.alternatives import match_occasion
from ai_fit.tools.human_routing import AuditLog
from ai_fit.tools.outcomes import OutcomeStore
from ai_fit.trace.builder import MASKED_FIELDS


# ---------------------------------------------------------------------------
# A tiny, hermetic in-memory store for the demo SKU + customer
# ---------------------------------------------------------------------------


def _build_small_store() -> DataStore:
    """Build a tiny :class:`DataStore` carrying just the demo SKU + customer.

    The store is constructed directly from a handful of raw rows (keyed by the
    normalized CSV column names the Task 2.2 mappers read) and its indexes are built
    via the real ``_build_indexes`` — so the full tool/mapper/validator path runs
    against real logic, just over a few rows instead of the production CSVs.

    The SKU leans clearly "too small" over its OMS fit returns and its catalog
    guidance says it runs small, so the demo's rubric-consistent stub decision (half
    size up, high confidence) passes the Guardrail Validator. The catalog is marked
    suited for ``work``, which the demo customer shops for, giving a positive
    occasion match.
    """
    sku = demo.DEMO_SKU
    customer_id = demo.DEMO_CUSTOMER_ID

    # Customer profile row (women's, usual 7.5, shops for work, reports shoes
    # often run too small). No completed measurement -> OMS drives the size.
    customer_row = {
        "synthetic_customer_id": customer_id,
        "shoe_sizing_system": "US Women",
        "usual_shoe_size": str(demo.DEMO_STATED_SIZE),
        "usual_shopping_occasions": "Work",
        "comfort_style_priority": "comfort",
        "measurements_completed": "0",
        "fit_shoes_often_too_small": "1",
        "fit_shoes_often_too_big": "0",
        "fit_shoes_too_narrow": "0",
        "fit_shoes_too_wide": "0",
        "fit_tight_around_toes": "0",
        "fit_heels_slip": "0",
        "fit_no_fit_issues": "0",
        "fit_not_sure": "0",
    }

    # Catalog rows: a few offered sizes for the same style, guidance "runs small",
    # suited for work (occasion match) + casual.
    style = "DEMOSTYLE"
    catalog_rows = [
        {
            "sku": sku if size == str(demo.DEMO_STATED_SIZE) else f"{sku}-{size}",
            "style": style,
            "size": size,
            "category": "heels",
            "price_usd": "79.99",
            "occasions": "work|casual",
            "fit_guidance": "Runs small - consider sizing up from your usual size",
            "fit_return_rate": "0.08",
            "toe_box": "regular",
            "gender_category": "women",
        }
        for size in ("7.0", "7.5", "8.0", "8.5")
    ]

    # OMS fit returns strongly leaning "too small" (-> size up), plus a quality
    # return so the counts are realistic.
    oms_returns_rows = [
        {"sku": sku, "order_number": f"ORD{i}", "reason": "Too small"}
        for i in range(12)
    ] + [
        {"sku": sku, "order_number": "ORDQ", "reason": "Defective"},
    ]

    # Orders so units_sold is populated (same OMS SKU key).
    oms_orders_rows = [
        {"sku": sku, "order_number": f"ORD{i}", "shipped": "10"} for i in range(5)
    ]

    # A couple of reviews leaning runs_small and tagged work.
    review_rows = [
        {"sku": sku, "fit_feedback": "runs_small", "width_feedback": "normal", "occasion": "work"},
        {"sku": sku, "fit_feedback": "runs_small", "width_feedback": "normal", "occasion": "casual"},
    ]

    store = DataStore(
        customer_profiles_rows=[customer_row],
        product_catalog_rows=catalog_rows,
        oms_returns_rows=oms_returns_rows,
        oms_orders_rows=oms_orders_rows,
        instore_exchanges_rows=[],
        customer_reviews_rows=review_rows,
    )
    store._build_indexes()
    return store


def _wire_demo_agent(store: DataStore) -> demo.DemoAgent:
    """Wire a :class:`demo.DemoAgent` over a small store + the default stub client.

    Mirrors :func:`ai_fit.demo.make_orchestrator` exactly (same tool specs, same
    stub-driven :class:`ConverseOrchestrator`) but against the injected small store,
    so no production CSV is ever loaded.
    """
    outcome_store = OutcomeStore()
    audit_log = AuditLog()
    tool_specs = demo.build_tool_specs(store, outcome_store, audit_log)
    orchestrator = ConverseOrchestrator(demo._default_stub(), tool_specs)
    return demo.DemoAgent(store=store, orchestrator=orchestrator, tool_specs=tool_specs)


# ---------------------------------------------------------------------------
# Config / model-wiring smoke tests (Requirement 9.1) — no data needed
# ---------------------------------------------------------------------------


def test_wired_to_claude_opus_on_bedrock_at_temperature_zero():
    """Agent is wired to a Claude Opus model id on Bedrock at temperature 0 (Req 9.1)."""
    assert "claude-opus" in config.BEDROCK_MODEL_ID.lower()
    assert config.BEDROCK_REGION
    assert config.BEDROCK_TEMPERATURE == 0.0


def test_tool_dispatch_timeout_and_retry_budget():
    """10s per-tool timeout and up to 2 retries (Requirements 9.6)."""
    assert config.TOOL_TIMEOUT_SECONDS == 10.0
    assert config.TOOL_MAX_RETRIES == 2


def test_orchestrator_defaults_to_config_wiring():
    """A ConverseOrchestrator defaults to the Claude Opus model id and temperature 0."""
    orch = ConverseOrchestrator(demo._default_stub(), [])
    assert orch._model_id == config.BEDROCK_MODEL_ID
    assert "claude-opus" in orch._model_id.lower()
    assert orch._temperature == 0.0


# ---------------------------------------------------------------------------
# build_tool_specs — the 10 Agent_Tools with correct names + schemas
# ---------------------------------------------------------------------------

_EXPECTED_TOOL_NAMES = {
    "get_sku_fit_signals",
    "get_customer_profile",
    "get_product_catalog_entry",
    "get_sku_reviews",
    "get_recommendation_outcomes",
    "get_quality_returns_and_units",
    "record_recommendation_outcome",
    "generate_quality_issue_report",
    "route_to_human_reviewer",
    "find_alternative_styles",
}


def test_build_tool_specs_produces_the_ten_tools():
    """build_tool_specs returns exactly the 10 named Agent_Tools (Requirement 9.2)."""
    store = _build_small_store()
    specs = demo.build_tool_specs(store, OutcomeStore(), AuditLog())

    assert len(specs) == 10
    names = [spec.name for spec in specs]
    assert len(names) == len(set(names)), "tool names must be unique"
    assert set(names) == _EXPECTED_TOOL_NAMES


def test_tool_specs_have_valid_input_schemas():
    """Every tool spec carries an object JSON schema with the required keys bound."""
    store = _build_small_store()
    specs = {s.name: s for s in demo.build_tool_specs(store, OutcomeStore(), AuditLog())}

    for spec in specs.values():
        assert spec.description
        assert callable(spec.handler)
        assert spec.input_schema.get("type") == "object"
        assert "properties" in spec.input_schema

    # Spot-check the argument shapes for a representative set of tools.
    assert specs["get_sku_fit_signals"].input_schema["required"] == ["sku"]
    assert specs["get_customer_profile"].input_schema["required"] == [
        "synthetic_customer_id"
    ]
    assert set(specs["record_recommendation_outcome"].input_schema["required"]) == {
        "sku",
        "recommended_size",
        "order_number",
    }
    assert set(specs["find_alternative_styles"].input_schema["required"]) == {
        "requested_occasion",
        "browsed_sku",
    }
    # The no-arg report tools expose an empty properties object.
    assert specs["get_quality_returns_and_units"].input_schema["properties"] == {}
    assert specs["generate_quality_issue_report"].input_schema["properties"] == {}


# ---------------------------------------------------------------------------
# End-to-end demo scenario against the stub client (no live AWS)
# ---------------------------------------------------------------------------


def _produce_demo_recommendation(agent: demo.DemoAgent):
    """Drive the demo SKU + customer through the orchestrator (stub-backed)."""
    return agent.orchestrator.produce_recommendation(
        demo.DEMO_SKU,
        stated_usual_size=demo.DEMO_STATED_SIZE,
        customer_id=demo.DEMO_CUSTOMER_ID,
    )


def test_demo_produces_size_recommendation_end_to_end():
    """The stub-driven loop yields a validated size recommendation (Req 7.2, 7.3)."""
    agent = _wire_demo_agent(_build_small_store())
    rec = _produce_demo_recommendation(agent)

    assert rec.ok, f"expected a recommendation, got failure: {rec.failure_reason}"
    assert rec.recommended_size is not None
    # Rubric-consistent stub sizes one half up from 7.5 (clear too-small lean).
    assert rec.recommended_size == pytest.approx(8.0)
    assert rec.confidence in {"low", "medium", "high"}
    # Explanation cites >= 1 historical signal (Requirement 7.2).
    assert isinstance(rec.explanation, str) and rec.explanation.strip()
    assert rec.supporting_signal_count >= 1


def test_demo_produces_occasion_match_result():
    """The direct occasion match reports a positive match on 'work' (Req 2.3)."""
    agent = _wire_demo_agent(_build_small_store())
    occasion_match = match_occasion(
        agent.store, demo.DEMO_SKU, direct_occasions=[demo.DEMO_OCCASION]
    )

    assert occasion_match["status"] == "match"
    assert demo.DEMO_OCCASION in occasion_match["matched_occasions"]
    assert demo.DEMO_OCCASION in occasion_match["customer_occasions"]


def test_demo_trace_is_masked_with_expected_fields():
    """The Reasoning_Trace masks customer_uid / cnsmr_prof_sid (Req 7.1, 7.4, 7.5)."""
    agent = _wire_demo_agent(_build_small_store())
    rec = _produce_demo_recommendation(agent)

    assert rec.ok
    trace = rec.trace
    assert trace is not None
    # The masked_fields list is exactly the two direct identifiers.
    assert trace["masked_fields"] == list(MASKED_FIELDS)
    assert trace["masked_fields"] == ["customer_uid", "cnsmr_prof_sid"]
    # The trace records ordered steps (Requirement 7.1).
    assert trace["steps"], "trace should record at least one ordered step"
    orders = [step["order"] for step in trace["steps"]]
    assert orders == sorted(orders)


def test_render_report_covers_explanation_confidence_occasion_and_trace():
    """render_report renders a string covering all four demo surfaces (Req 7.4)."""
    agent = _wire_demo_agent(_build_small_store())
    rec = _produce_demo_recommendation(agent)
    occasion_match = match_occasion(
        agent.store, demo.DEMO_SKU, direct_occasions=[demo.DEMO_OCCASION]
    )

    report = demo.render_report(rec, occasion_match)

    assert isinstance(report, str) and report
    # Covers the size recommendation + confidence + customer-facing explanation.
    assert "SIZE RECOMMENDATION" in report
    assert "Confidence" in report
    assert rec.explanation in report
    # Covers the occasion match.
    assert "OCCASION MATCH" in report
    assert demo.DEMO_OCCASION in report
    # Covers the masked Reasoning_Trace.
    assert "MASKED REASONING TRACE" in report
    assert "customer_uid" in report and "cnsmr_prof_sid" in report
