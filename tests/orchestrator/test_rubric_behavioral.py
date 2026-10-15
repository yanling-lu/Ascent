"""Example/behavioral tests for Claude rubric adherence and stability (Task 12.4).

These tests exercise the **full orchestrator path** — Claude's structured decision
-> structured-output parser -> deterministic Guardrail Validator -> masked
Reasoning_Trace -> :class:`~ai_fit.orchestrator.converse.Recommendation` — on a set
of representative scenarios, and assert what the system guarantees *regardless of
the model*. The Bedrock call sits behind the thin
:class:`~ai_fit.orchestrator.converse.BedrockConverseClient` seam, so instead of
calling live Bedrock we drive the loop with a scripted stub client that:

1. first returns a ``tool_use`` turn requesting the Agent_Tools a size
   recommendation requires (``get_sku_fit_signals``, ``get_product_catalog_entry``,
   ``get_sku_reviews``, and optionally ``get_customer_profile``), then
2. returns a final assistant turn carrying a JSON decision whose
   ``recommended_size`` / ``confidence_level`` the scenario controls.

The :class:`ToolSpec` handlers are small canned functions returning exactly the
OMS / catalog / profile / review data the scenario needs. We then assert on the
**validated** :class:`Recommendation`: its size, confidence, advisory flag,
customer-facing explanation (which must cite >=1 historical signal), and the
masked trace. This is deliberately a behavioral test of the deterministic layers
that wrap the model, not a test of the model's prose.

Scenarios (mapping to Requirements 1.9, 1.10, 10.4, 7.2):

* **A — clear too-small OMS lean, agreeing catalog guidance** -> one half size up
  at the expected confidence tier (OMS dominance + base confidence tier).
* **B — present foot measurement** -> the measurement lookup overrides the proposed
  size (measurement precedence, Req 1.11).
* **C — over-large proposed shift** -> capped to one net half size (Req 1.5/1.6).
* **D — sparse OMS SKU** -> confidence capped at medium (Req 1.19/3.4).
* **E — determinism** -> identical inputs produce identical size + confidence
  (Req 10.4).
* **F — explanation + trace** -> every recommendation carries a customer-facing
  explanation citing >=1 historical signal and a masked trace (Req 7.2, 1.10).

The orchestrator's ``sleep`` seam is overridden with a no-op so no scenario incurs
a real delay.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from ai_fit.orchestrator.converse import (
    ConverseOrchestrator,
    Recommendation,
    ToolSpec,
)
from ai_fit.trace.builder import MASK_PLACEHOLDER, MASKED_FIELDS

# ---------------------------------------------------------------------------
# A scripted Bedrock Converse stub (no live Bedrock)
# ---------------------------------------------------------------------------


def _tool_use_turn(tool_names: List[str]) -> Dict[str, Any]:
    """Build a Converse ``tool_use`` response requesting each named tool.

    Mirrors the ``{"output": {"message": {...}}, "stopReason": "tool_use"}`` shape
    the orchestrator reads: one ``toolUse`` content block per tool, each with a
    distinct ``toolUseId`` and a ``{"sku": ...}`` input (the canned handlers ignore
    their arguments, so the input only has to be well-formed).
    """
    content = [
        {
            "toolUse": {
                "toolUseId": f"tu-{i}",
                "name": name,
                "input": {"sku": "SKU-1", "customer_id": "CUST-1"},
            }
        }
        for i, name in enumerate(tool_names)
    ]
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": "tool_use",
    }


def _final_decision_turn(decision: Dict[str, Any]) -> Dict[str, Any]:
    """Build a Converse final turn carrying the structured decision as JSON text."""
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": json.dumps(decision)}],
            }
        },
        "stopReason": "end_turn",
    }


class ScriptedConverseClient:
    """A :class:`BedrockConverseClient` that replays a fixed list of responses.

    Each ``converse`` call pops the next scripted response. The orchestrator calls
    ``converse`` once per loop turn, so a two-element script (``[tool_use_turn,
    final_turn]``) drives exactly one tool-use round followed by the final decision
    — enough to exercise the whole parse -> validate -> trace path with the data the
    scenario's handlers return. The client records how many calls it received so a
    determinism test can confirm each ``produce_recommendation`` ran the loop
    independently.
    """

    def __init__(self, responses: List[Dict[str, Any]]) -> None:
        self._responses = list(responses)
        self._index = 0
        self.calls = 0

    def converse(self, **_kwargs: Any) -> Dict[str, Any]:
        self.calls += 1
        if self._index >= len(self._responses):
            # Defensive: a well-formed script ends on a non-tool_use turn, so the
            # loop should never ask for more. If it does, repeat the last turn.
            return self._responses[-1]
        response = self._responses[self._index]
        self._index += 1
        return response


# ---------------------------------------------------------------------------
# Canned tool handlers + ToolSpec builders
# ---------------------------------------------------------------------------

_DUMMY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {"sku": {"type": "string"}},
    "required": ["sku"],
}


def _spec(name: str, value: Dict[str, Any]) -> ToolSpec:
    """A ToolSpec whose handler ignores its arguments and returns ``value``.

    The handler accepts ``**kwargs`` because the orchestrator forwards Claude's
    ``input`` dict as keyword arguments; the canned data does not depend on them.
    """

    def handler(**_kwargs: Any) -> Dict[str, Any]:
        return value

    return ToolSpec(
        name=name, description=f"canned {name}", input_schema=_DUMMY_SCHEMA, handler=handler
    )


def _build_specs(
    *,
    fit: Dict[str, Any],
    catalog: Dict[str, Any],
    reviews: Optional[Dict[str, Any]] = None,
    profile: Optional[Dict[str, Any]] = None,
    outcomes: Optional[Dict[str, Any]] = None,
) -> List[ToolSpec]:
    """Build the ToolSpec list a size recommendation needs, with canned data."""
    specs = [
        _spec("get_sku_fit_signals", fit),
        _spec("get_product_catalog_entry", catalog),
        _spec("get_sku_reviews", reviews if reviews is not None else {}),
    ]
    if profile is not None:
        specs.append(_spec("get_customer_profile", profile))
    if outcomes is not None:
        specs.append(_spec("get_recommendation_outcomes", outcomes))
    return specs


def _orchestrator(
    specs: List[ToolSpec], client: ScriptedConverseClient
) -> ConverseOrchestrator:
    """Build an orchestrator wired to the stub client with the sleep seam disabled."""
    return ConverseOrchestrator(
        client=client,
        tool_specs=specs,
        sleep=lambda _seconds: None,  # no real delay in tests
    )


# A dense, standard offered-size grid used by most scenarios.
_OFFERED = [6.0, 6.5, 7.0, 7.5, 8.0, 8.5, 9.0]


# ---------------------------------------------------------------------------
# Scenario A — clear too-small OMS lean + agreeing catalog guidance -> +0.5, high
# ---------------------------------------------------------------------------


def test_clear_too_small_lean_recommends_one_half_up_at_high_confidence():
    """A dominant 'too small' OMS lean with agreeing catalog guidance -> +0.5 size.

    The scenario gives a well-populated SKU (>=15 directional fit signals so the
    base confidence tier is high) leaning strongly 'too small', with catalog
    guidance that agrees ('runs small'). Claude proposes 8.0 (one half up from the
    stated 7.5) at high confidence; the validator keeps it. The system must return
    one half size up at high confidence (OMS dominance direction + base tier).
    """
    fit = {
        "too_big_count": 1,
        "too_small_count": 18,  # strong 'too small' dominance, >=15 total signals
        "uncomfortable_count": 0,
        "exchange_count": 2,
    }
    catalog = {"offered_sizes": _OFFERED, "fit_guidance": "runs small"}
    reviews = {"width_signal_counts": {"narrow": 0, "wide": 0, "normal": 0}}
    specs = _build_specs(fit=fit, catalog=catalog, reviews=reviews)

    client = ScriptedConverseClient(
        [
            _tool_use_turn(
                ["get_sku_fit_signals", "get_product_catalog_entry", "get_sku_reviews"]
            ),
            _final_decision_turn(
                {
                    "recommended_size": "8.0",
                    "confidence_level": "high",
                    "size_rationale": "Most buyers found this style runs small.",
                    "cited_signals": ["oms_fit_returns", "catalog_fit_guidance"],
                }
            ),
        ]
    )

    rec = _orchestrator(specs, client).produce_recommendation(
        "SKU-1", stated_usual_size=7.5
    )

    assert rec.ok
    assert rec.recommended_size == 8.0  # one half size up from 7.5
    assert rec.confidence == "high"
    assert rec.advisory is False


# ---------------------------------------------------------------------------
# Scenario B — present foot measurement overrides the proposed size (Req 1.11)
# ---------------------------------------------------------------------------


def test_present_foot_measurement_overrides_proposed_size():
    """A present Foot_Measurement sets the size via measurement lookup (Req 1.11).

    The customer profile carries a 24.0 cm foot length in women's sizing, which the
    measurement table maps to 7.0. Claude is deliberately made to propose a wrong
    size (9.0); measurement precedence must override it, so the validated size is
    7.0 regardless of the proposal, and the trace records a 'measurement_precedence'
    correction.
    """
    fit = {
        "too_big_count": 2,
        "too_small_count": 20,  # a lean that would otherwise push the size up
        "uncomfortable_count": 0,
        "exchange_count": 0,
    }
    catalog = {"offered_sizes": _OFFERED, "fit_guidance": "runs small"}
    profile = {
        "sizing_system": "Women's sizing",
        "usual_shoe_size": 8.5,
        "foot_length_cm": 24.0,  # -> women's 7.0 via the measurement table
        "customer_uid": "UID-SECRET",  # must be masked out of the trace
    }
    specs = _build_specs(
        fit=fit, catalog=catalog, reviews={}, profile=profile
    )

    client = ScriptedConverseClient(
        [
            _tool_use_turn(
                [
                    "get_sku_fit_signals",
                    "get_product_catalog_entry",
                    "get_sku_reviews",
                    "get_customer_profile",
                ]
            ),
            _final_decision_turn(
                {
                    "recommended_size": "9.0",  # deliberately wrong
                    "confidence_level": "high",
                    "size_rationale": "Based on the recorded foot measurement.",
                    "cited_signals": ["foot_measurement"],
                }
            ),
        ]
    )

    rec = _orchestrator(specs, client).produce_recommendation(
        "SKU-1", stated_usual_size=8.5, customer_id="CUST-1"
    )

    assert rec.ok
    assert rec.recommended_size == 7.0  # measurement lookup, not the proposed 9.0
    invariants = {
        c["invariant"] for c in rec.trace["validator_corrections"]
    }
    assert "measurement_precedence" in invariants


# ---------------------------------------------------------------------------
# Scenario C — over-large proposed shift is capped to one half size (Req 1.5/1.6)
# ---------------------------------------------------------------------------


def test_over_large_proposed_shift_is_capped_to_one_half_size():
    """A proposed +2.0 shift is clamped to a net one half size up (Req 1.5/1.6).

    No measurement is present, so the non-measurement net single half-size cap
    applies. Claude proposes 9.5 (four half sizes above the stated 7.5); the
    validator must clamp it to 8.0 (one half size up) and record a 'half_size_cap'
    correction.
    """
    fit = {
        "too_big_count": 0,
        "too_small_count": 16,
        "uncomfortable_count": 0,
        "exchange_count": 0,
    }
    catalog = {"offered_sizes": _OFFERED, "fit_guidance": "runs small"}
    specs = _build_specs(fit=fit, catalog=catalog, reviews={})

    client = ScriptedConverseClient(
        [
            _tool_use_turn(
                ["get_sku_fit_signals", "get_product_catalog_entry", "get_sku_reviews"]
            ),
            _final_decision_turn(
                {
                    "recommended_size": "9.5",  # +4 half sizes — way over the cap
                    "confidence_level": "high",
                    "size_rationale": "Runs very small.",
                    "cited_signals": ["oms_fit_returns"],
                }
            ),
        ]
    )

    rec = _orchestrator(specs, client).produce_recommendation(
        "SKU-1", stated_usual_size=7.5
    )

    assert rec.ok
    assert rec.recommended_size == 8.0  # capped to one half size up
    assert abs(rec.recommended_size - 7.5) <= 0.5
    invariants = {c["invariant"] for c in rec.trace["validator_corrections"]}
    assert "half_size_cap" in invariants


# ---------------------------------------------------------------------------
# Scenario D — sparse OMS SKU is capped at medium confidence (Req 1.19/3.4)
# ---------------------------------------------------------------------------


def test_sparse_oms_sku_is_capped_at_medium_confidence():
    """A SKU with OMS signals below the threshold is capped at medium (Req 1.19/3.4).

    Only 3 directional fit signals (< the 5 Minimum_Signal_Threshold) means the
    recommendation is catalog-/review-driven and sparse. Claude over-reaches and
    proposes 'high' confidence; the validator must cap it at 'medium' and record a
    'sparse_confidence_cap' correction.
    """
    fit = {
        "too_big_count": 0,
        "too_small_count": 3,  # sparse: below the Minimum_Signal_Threshold of 5
        "uncomfortable_count": 0,
        "exchange_count": 0,
    }
    catalog = {"offered_sizes": _OFFERED, "fit_guidance": "runs small"}
    specs = _build_specs(fit=fit, catalog=catalog, reviews={})

    client = ScriptedConverseClient(
        [
            _tool_use_turn(
                ["get_sku_fit_signals", "get_product_catalog_entry", "get_sku_reviews"]
            ),
            _final_decision_turn(
                {
                    "recommended_size": "7.5",
                    "confidence_level": "high",  # over-reaching on sparse data
                    "size_rationale": "Limited history, catalog says runs small.",
                    "cited_signals": ["catalog_fit_guidance"],
                }
            ),
        ]
    )

    rec = _orchestrator(specs, client).produce_recommendation(
        "SKU-1", stated_usual_size=7.5
    )

    assert rec.ok
    assert rec.confidence == "medium"  # sparse cap
    invariants = {c["invariant"] for c in rec.trace["validator_corrections"]}
    assert "sparse_confidence_cap" in invariants


# ---------------------------------------------------------------------------
# Scenario E — identical inputs produce identical recommendations (Req 10.4)
# ---------------------------------------------------------------------------


def _stability_setup():
    """Build the specs + scripted turns shared by both runs of the stability test."""
    fit = {
        "too_big_count": 1,
        "too_small_count": 17,
        "uncomfortable_count": 0,
        "exchange_count": 1,
    }
    catalog = {"offered_sizes": _OFFERED, "fit_guidance": "runs small"}
    specs = _build_specs(fit=fit, catalog=catalog, reviews={})
    turns = [
        _tool_use_turn(
            ["get_sku_fit_signals", "get_product_catalog_entry", "get_sku_reviews"]
        ),
        _final_decision_turn(
            {
                "recommended_size": "8.0",
                "confidence_level": "high",
                "size_rationale": "Runs small per fit history.",
                "cited_signals": ["oms_fit_returns"],
            }
        ),
    ]
    return specs, turns


def test_identical_inputs_produce_identical_recommendations():
    """Running the full path twice on identical inputs yields identical output (Req 10.4).

    Determinism is what temperature 0 plus the deterministic validator layering
    buys the system. With the same scripted model turns and the same canned tool
    data, two independent ``produce_recommendation`` calls must agree on size and
    confidence. Each call drives its own loop (the stub is replayed fresh), so this
    checks end-to-end stability, not just validator purity.
    """
    specs, turns = _stability_setup()

    rec1 = _orchestrator(specs, ScriptedConverseClient(turns)).produce_recommendation(
        "SKU-1", stated_usual_size=7.5
    )
    rec2 = _orchestrator(specs, ScriptedConverseClient(turns)).produce_recommendation(
        "SKU-1", stated_usual_size=7.5
    )

    assert rec1.ok and rec2.ok
    assert rec1.recommended_size == rec2.recommended_size
    assert rec1.confidence == rec2.confidence


# ---------------------------------------------------------------------------
# Scenario F — explanation cites a historical signal + trace is masked (Req 7.2)
# ---------------------------------------------------------------------------


def test_recommendation_cites_historical_signal_and_masks_trace():
    """Every recommendation carries a historical-signal explanation + masked trace.

    The explanation must be plain language and cite >=1 historical signal
    (Req 7.2); here the model cites OMS fit returns and exchanges, so the
    explanation names 'past fit-related returns' and 'past exchanges'. The trace
    must declare its masked fields and must not leak the direct customer identifier
    the profile carried (``customer_uid`` -> ``***MASKED***``, Req 7.5/10.3). The
    supporting-signal count must reflect the real signals used, including outcomes
    (Req 1.10, 4.4).
    """
    fit = {
        "too_big_count": 1,
        "too_small_count": 18,
        "uncomfortable_count": 0,
        "exchange_count": 2,  # 21 total OMS signals
    }
    catalog = {"offered_sizes": _OFFERED, "fit_guidance": "runs small"}
    profile = {
        "sizing_system": "Women's sizing",
        "usual_shoe_size": 7.5,
        "customer_uid": "UID-SECRET-123",  # must be masked out of the trace
        "cnsmr_prof_sid": "SID-SECRET-456",  # must be masked out of the trace
    }
    outcomes = {"outcome_count": 4}  # reinforcing outcome signals (Req 4.4)
    specs = _build_specs(
        fit=fit, catalog=catalog, reviews={}, profile=profile, outcomes=outcomes
    )

    client = ScriptedConverseClient(
        [
            _tool_use_turn(
                [
                    "get_sku_fit_signals",
                    "get_product_catalog_entry",
                    "get_sku_reviews",
                    "get_customer_profile",
                    "get_recommendation_outcomes",
                ]
            ),
            _final_decision_turn(
                {
                    "recommended_size": "8.0",
                    "confidence_level": "high",
                    "size_rationale": "Buyers consistently found this runs small.",
                    "cited_signals": ["oms_fit_returns", "oms_exchanges"],
                }
            ),
        ]
    )

    rec = _orchestrator(specs, client).produce_recommendation(
        "SKU-1", stated_usual_size=7.5, customer_id="CUST-1"
    )

    assert rec.ok
    # Customer-facing explanation cites at least one historical signal (Req 7.2).
    assert rec.explanation.strip()
    assert "past fit-related returns" in rec.explanation
    assert "based on" in rec.explanation.lower()

    # The trace is present and declares its masked fields (Req 7.5/10.3).
    assert rec.trace is not None
    assert set(rec.trace["masked_fields"]) == set(MASKED_FIELDS)

    # No cleartext direct identifier survives anywhere in the serialized trace.
    serialized = json.dumps(rec.trace)
    assert "UID-SECRET-123" not in serialized
    assert "SID-SECRET-456" not in serialized
    assert MASK_PLACEHOLDER in serialized

    # Supporting-signal count includes OMS (21) + outcome (4) signals (Req 1.10, 4.4).
    assert rec.supporting_signal_count == 25


if __name__ == "__main__":  # pragma: no cover - convenience for direct runs
    raise SystemExit(pytest.main([__file__, "-v"]))
