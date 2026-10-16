"""Serialization / contract tests for ``POST /api/recommendation`` (Task 2.2).

These are the design's "Serialization / contract tests (service)" for the thin
Recommendation_Service pass-through layer. They assert the single guarantee the
service exists to make: it serializes the agent's :class:`~ai_fit.orchestrator.converse.Recommendation`
**field-for-field** with :func:`dataclasses.asdict` and attaches the occasion match
unchanged — adding, removing, renaming, or altering nothing, and never unmasking the
trace or leaking a Protected_Attribute.

Covered contracts:

* **No mutation of the Recommendation (Req 7.4)** — given a known ``Recommendation``
  with a populated *masked* ``trace``, the response ``recommendation`` sub-object
  equals ``dataclasses.asdict(recommendation)`` exactly, with ``trace.masked_fields``
  and the ``***MASKED***`` redaction placeholder preserved verbatim.
* **No Protected_Attribute leakage (Reqs 1.5, 2.2)** — the whole
  ``/api/recommendation`` payload contains none of the protected keys
  (age, gender, ethnicity, nationality, location) at *any* nesting depth.
* **Failure pass-through (Req 8.1)** — given ``Recommendation(ok=False,
  failure_reason=...)``, the failure fields pass through and no size/confidence is
  fabricated.

Hermeticity: ``create_app`` normally builds the real agent via
``ai_fit.demo.make_orchestrator`` (which loads the ~1M-row production CSVs) and reads
``offered_sizes`` from the catalog. To keep these contract tests fast and fully
offline — and to inject a *known* ``Recommendation`` so exact ``asdict`` equality is
assertable — the service's ``make_orchestrator``, ``get_product_catalog_entry``, and
``match_occasion`` seams are monkeypatched. No live AWS, no production dataset, and no
real Bedrock call is involved (Stub_Mode intent, Req 8.3).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, Iterable, List

import pytest

from ai_fit.orchestrator.converse import Recommendation
from ai_fit.trace.builder import MASK_PLACEHOLDER, MASKED_FIELDS
from ai_fit_demo_ui import service


# ---------------------------------------------------------------------------
# Protected-attribute scanner (Reqs 1.5, 2.2)
# ---------------------------------------------------------------------------

# The five Protected_Attributes that must never appear anywhere in a payload. The
# data layer already strips these; the scanner is the independent check that the
# field-for-field serialization did not reintroduce one at any depth.
PROTECTED_KEYS = {"age", "gender", "ethnicity", "nationality", "location"}


def _protected_keys_at_any_depth(value: Any) -> List[str]:
    """Return every protected key found anywhere in ``value`` (recursively).

    Walks dicts, lists, and tuples to any nesting depth. A dict key is flagged when
    its lowercased form matches a Protected_Attribute exactly, so legitimately
    retained fields that merely *contain* a protected word (e.g. ``gender_category``)
    are not false positives — only an exact ``gender``/``age``/... key is a leak.
    Scalars never match.
    """
    found: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, inner in node.items():
                if isinstance(key, str) and key.lower() in PROTECTED_KEYS:
                    found.append(key.lower())
                walk(inner)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(value)
    return found


# ---------------------------------------------------------------------------
# A fake agent that returns a *known* Recommendation (no CSVs, no AWS)
# ---------------------------------------------------------------------------


class _FakeOrchestrator:
    """Stand-in orchestrator whose ``produce_recommendation`` returns a canned result."""

    def __init__(self, recommendation: Recommendation) -> None:
        self._recommendation = recommendation
        self.calls: List[Dict[str, Any]] = []

    def produce_recommendation(self, sku: str, **kwargs: Any) -> Recommendation:
        self.calls.append({"sku": sku, **kwargs})
        return self._recommendation


class _FakeAgent:
    """Minimal ``DemoAgent`` stand-in: an orchestrator plus a sentinel store."""

    def __init__(self, recommendation: Recommendation) -> None:
        self.orchestrator = _FakeOrchestrator(recommendation)
        self.store = object()  # opaque sentinel; match_occasion is patched out


def _known_masked_trace() -> Dict[str, Any]:
    """A populated, already-masked trace mirroring ``ReasoningTrace.full_trace()``.

    The two direct identifiers are redacted to the real ``***MASKED***`` placeholder,
    nested inside a step's data, so the test can assert the placeholder and
    ``masked_fields`` survive the serialization verbatim (Reqs 7.4, 7.5).
    """
    return {
        "steps": [
            {
                "order": 1,
                "step_type": "tool_invocation",
                "tool_name": "get_customer_profile",
                "data_summary": {
                    # Masked identifiers appear verbatim as the redaction placeholder.
                    "customer_uid": MASK_PLACEHOLDER,
                    "cnsmr_prof_sid": MASK_PLACEHOLDER,
                    "usual_shoe_size": "7.5",
                },
                "decision": None,
            },
            {
                "order": 2,
                "step_type": "decision",
                "tool_name": None,
                "data_summary": {"recommended_size": 8.0},
                "decision": "size up a half from usual",
            },
        ],
        "validator_corrections": [
            {
                "invariant": "size_within_offered_range",
                "proposed_value": 8.0,
                "corrected_value": 8.0,
            }
        ],
        "customer_facing_explanation": "This style runs small, so a half size up fits better.",
        "failed_tools": [
            {"tool_name": "get_recommendation_outcomes", "unavailable_data": "no prior outcomes"}
        ],
        "masked_fields": list(MASKED_FIELDS),
    }


def _known_recommendation() -> Recommendation:
    """A fully-populated successful ``Recommendation`` carrying the masked trace."""
    return Recommendation(
        ok=True,
        recommended_size=8.0,
        confidence="high",
        advisory=False,
        explanation="This style has a strong history of running small; size up a half.",
        supporting_signal_count=102,
        warnings=["This style runs narrow; consider the roomier toe box."],
        note=None,
        trace=_known_masked_trace(),
        failure_reason=None,
    )


def _failure_recommendation() -> Recommendation:
    """A failed ``Recommendation`` with no fabricated size/confidence (Req 8.1)."""
    return Recommendation(
        ok=False,
        failure_reason="bedrock_throttled",
        explanation="We couldn't produce a fit recommendation right now. Please retry.",
    )


# ---------------------------------------------------------------------------
# App builder: wire the known Recommendation into a real Flask app
# ---------------------------------------------------------------------------


def _build_client(monkeypatch: pytest.MonkeyPatch, recommendation: Recommendation):
    """Build a test client for an app wired to return ``recommendation``.

    Patches the three ``service`` seams that would otherwise touch the production
    datasets / a live orchestrator:

    * ``make_orchestrator`` -> returns a :class:`_FakeAgent` holding the known rec,
    * ``get_product_catalog_entry`` -> a catalog entry so ``offered_sizes`` resolves,
    * ``match_occasion`` -> a fixed occasion-match dict attached unchanged.
    """
    fake_agent = _FakeAgent(recommendation)

    monkeypatch.setattr(service, "make_orchestrator", lambda client: fake_agent)
    monkeypatch.setattr(
        service,
        "get_product_catalog_entry",
        lambda store, sku: {"found": True, "offered_sizes": ["7", "7.5", "8", "8.5"]},
    )
    # The demo scopes the occasion match to the product's occasions (special
    # occasion for the Stessy2.0). Use a "special occasion" match so it passes
    # through the scoping unchanged and the serialization assertions below stay
    # focused on the recommendation contract (the scoping itself is covered in
    # test_demo_ui_alternatives_config.py).
    occasion_match = {
        "status": "match",
        "matched_occasions": ["special occasion"],
        "customer_occasions": ["special occasion"],
        "sku_occasions": ["special occasion"],
        "review_cues_used": [],
        "unrecognized_values": [],
        "message": "This product matches your occasion: special occasion.",
    }
    monkeypatch.setattr(
        service,
        "match_occasion",
        lambda store, sku, **kwargs: occasion_match,
    )

    app = service.create_app(stub=True)
    app.config.update(TESTING=True)
    return app.test_client(), fake_agent


def _post_recommendation(client, **body: Any):
    """POST a recommendation request body and return (status_code, json)."""
    response = client.post("/api/recommendation", json=body)
    return response.status_code, response.get_json()


# ---------------------------------------------------------------------------
# Contract 1 — field-for-field serialization, masked trace preserved (Req 7.4)
# ---------------------------------------------------------------------------


def test_recommendation_subobject_equals_asdict_with_trace_preserved(monkeypatch):
    """The response recommendation equals ``asdict(recommendation)`` exactly (Req 7.4)."""
    recommendation = _known_recommendation()
    client, _ = _build_client(monkeypatch, recommendation)

    status, payload = _post_recommendation(
        client, sku="15887957", size=7, customer_id="ALDO_SYN_0005", occasion="work"
    )

    assert status == 200
    expected = asdict(recommendation)

    # Field-for-field equality: nothing added, removed, renamed, or altered.
    assert payload["recommendation"] == expected

    # The masked trace survives verbatim: masked_fields and the redaction
    # placeholder are preserved exactly (Reqs 7.4, 7.5).
    trace = payload["recommendation"]["trace"]
    assert trace["masked_fields"] == list(MASKED_FIELDS)
    step_data = trace["steps"][0]["data_summary"]
    assert step_data["customer_uid"] == MASK_PLACEHOLDER
    assert step_data["cnsmr_prof_sid"] == MASK_PLACEHOLDER

    # The occasion match is attached unchanged alongside the recommendation.
    assert payload["occasion_match"]["status"] == "match"
    assert payload["occasion_match"]["matched_occasions"] == ["special occasion"]


# ---------------------------------------------------------------------------
# Contract 2 — no Protected_Attribute at any depth (Reqs 1.5, 2.2)
# ---------------------------------------------------------------------------


def test_payload_has_no_protected_keys_at_any_depth(monkeypatch):
    """The whole payload carries no protected key at any nesting depth (Reqs 1.5, 2.2)."""
    client, _ = _build_client(monkeypatch, _known_recommendation())

    status, payload = _post_recommendation(
        client, sku="15887957", size=7, customer_id="ALDO_SYN_0005", occasion="work"
    )

    assert status == 200
    leaks = _protected_keys_at_any_depth(payload)
    assert leaks == [], f"protected keys leaked into the payload: {leaks}"


def test_protected_scanner_detects_a_nested_leak():
    """Guard the scanner itself: it flags a protected key nested at any depth."""
    assert _protected_keys_at_any_depth({"a": {"b": [{"gender": "x"}]}}) == ["gender"]
    # A field that merely contains a protected word is not a leak.
    assert _protected_keys_at_any_depth({"gender_category": "women"}) == []


# ---------------------------------------------------------------------------
# Contract 3 — failure pass-through, no fabricated size/confidence (Req 8.1)
# ---------------------------------------------------------------------------


def test_failure_recommendation_passes_through_with_no_fabricated_fields(monkeypatch):
    """``ok=False`` returns the failure verbatim and fabricates no size/confidence (Req 8.1)."""
    recommendation = _failure_recommendation()
    client, _ = _build_client(monkeypatch, recommendation)

    status, payload = _post_recommendation(
        client, sku="15887957", size=7, customer_id="ALDO_SYN_0005", occasion="work"
    )

    # A failure still returns 200 with the serialized Recommendation (design/Req 8.1).
    assert status == 200
    rec = payload["recommendation"]

    # The failure fields pass through field-for-field.
    assert rec == asdict(recommendation)
    assert rec["ok"] is False
    assert rec["failure_reason"] == "bedrock_throttled"

    # No fabricated size/confidence: both are the dataclass defaults (null).
    assert rec["recommended_size"] is None
    assert rec["confidence"] is None

    # Even the failure payload carries no Protected_Attribute.
    assert _protected_keys_at_any_depth(payload) == []
