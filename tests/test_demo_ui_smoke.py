"""End-to-end single-command Stub_Mode smoke test (Task 11.1).

This is the one end-to-end smoke test the design's "Smoke test" strategy calls for:
single-command startup in Stub_Mode, one ``POST /api/recommendation`` that returns a
``200`` with a real recommendation and a **visible masked trace**, and the
Prototype_Disclaimer + Demo_Guide present in the served page (Requirements 9.1, 1.3,
1.4, 8.3, 9.2).

Unlike the narrower service/endpoint tests, this one deliberately exercises the
**true single-command path**: it drives ``ai_fit_demo_ui.service.create_app(stub=True)``
— the exact factory ``python -m ai_fit_demo_ui --stub`` / ``AI_FIT_DEMO_STUB=1``
invokes — so the real route registration, the real ``make_orchestrator`` wiring, the
real ``build_stub_client`` canned client, the real ``ConverseOrchestrator`` tool-use
loop, the Guardrail Validator, and the real Reasoning_Trace masking all run end to
end. Nothing about the recommendation, trace, or masking is stubbed by the test.

Hermeticity / speed: ``make_orchestrator`` loads the ~1M-row production CSVs via
``DataStore.load`` (~75-90s cold). To keep the smoke test fast and fully offline we
monkeypatch **only** ``DataStore.load`` to return a tiny in-memory store carrying the
demo SKU + customer (the same hermetic-store pattern the other demo-ui tests use).
Everything downstream of the load — the stub client, the orchestrator loop, and the
trace masking — is the real code path. No AWS, no Bedrock, no network.

The "no Bedrock / network" guarantee (Requirements 8.3, 9.2) is asserted structurally
by installing an *exploding* ``BotoBedrockConverseClient`` that raises if the live
client is ever constructed; in Stub_Mode it must never be, and the recommendation
still succeeds purely through the canned stub.
"""

from __future__ import annotations

import pytest

from ai_fit import demo
from ai_fit.data.store import DataStore
from ai_fit.orchestrator import BotoBedrockConverseClient
from ai_fit.trace.builder import MASK_PLACEHOLDER, MASKED_FIELDS

from ai_fit_demo_ui import service
from ai_fit_demo_ui.service import DEMO_GUIDE_STEPS, PROTOTYPE_DISCLAIMER


# ---------------------------------------------------------------------------
# A tiny, hermetic in-memory store for the demo SKU + customer
# ---------------------------------------------------------------------------


def _build_small_store() -> DataStore:
    """Build a tiny :class:`DataStore` carrying just the demo SKU + customer.

    Mirrors the hermetic store the other demo-ui tests use: a handful of raw rows
    keyed by the normalized CSV column names, with the real ``_build_indexes`` run so
    the tool/mapper path works against real logic over a few rows (not the production
    CSVs). The SKU leans "runs small" with ``work`` suitability, consistent with the
    rubric-aligned Stub_Mode decision (a half size up from the usual 7.5), so the real
    orchestrator loop + validator produce an ``ok`` recommendation offline.
    """
    sku = demo.DEMO_SKU
    customer_id = demo.DEMO_CUSTOMER_ID

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

    oms_returns_rows = [
        {"sku": sku, "order_number": f"ORD{i}", "reason": "Too small"}
        for i in range(12)
    ] + [{"sku": sku, "order_number": "ORDQ", "reason": "Defective"}]

    oms_orders_rows = [
        {"sku": sku, "order_number": f"ORD{i}", "shipped": "10"} for i in range(5)
    ]

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


class _ExplodingBedrockClient:
    """Stand-in for ``BotoBedrockConverseClient`` that fails if ever constructed.

    In Stub_Mode the service must wire the canned stub client and *never* build the
    live Bedrock client (which would open an AWS/network client). Installing this as
    the client the service sees turns any accidental live wiring into a loud failure
    instead of a silent network call (Requirements 8.3, 9.2).
    """

    def __init__(self, *args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError(
            "Stub_Mode must not construct a live Bedrock client (no AWS/network)."
        )


# ---------------------------------------------------------------------------
# Fixtures — the real single-command app, hermetic store, offline
# ---------------------------------------------------------------------------


@pytest.fixture()
def smoke_app(monkeypatch):
    """Build the real single-command Stub_Mode app over a hermetic store (no AWS).

    Patches only ``DataStore.load`` (so ``make_orchestrator`` resolves the tiny store
    instead of the production CSVs) and installs an exploding live-Bedrock client to
    prove none is constructed. Everything else — ``create_app(stub=True)``,
    ``build_stub_client``, the orchestrator loop, and trace masking — is the real
    single-command path.
    """
    monkeypatch.setattr(
        demo.DataStore, "load", staticmethod(lambda dataset_paths=None: _build_small_store())
    )
    monkeypatch.setattr(demo, "BotoBedrockConverseClient", _ExplodingBedrockClient)

    # This is exactly what `python -m ai_fit_demo_ui --stub` builds.
    app = service.create_app(stub=True)
    app.testing = True
    return app


@pytest.fixture()
def smoke_client(smoke_app):
    return smoke_app.test_client()


# ---------------------------------------------------------------------------
# One POST /api/recommendation -> 200 with recommendation + visible masked trace
# ---------------------------------------------------------------------------


def test_single_command_stub_mode_recommendation_has_visible_masked_trace(smoke_app, smoke_client):
    """One recommendation returns 200 with ``ok`` and a visible masked trace (Reqs 9.1, 9.2, 8.3).

    Drives the real orchestrator loop through the canned stub client end to end and
    asserts the response is a serialized ``Recommendation`` (``ok`` true) whose
    ``trace`` is a *visible masked trace*: ``trace.masked_fields`` lists the redacted
    identifiers and the ``***MASKED***`` placeholder is preserved verbatim wherever a
    masked identifier appeared.
    """
    # Sanity: the app really was built in Stub_Mode (the single-command stub path).
    assert smoke_app.config["AI_FIT_STUB_MODE"] is True

    resp = smoke_client.post(
        "/api/recommendation",
        json={
            "sku": demo.DEMO_SKU,
            "size": demo.DEMO_STATED_SIZE,
            "customer_id": demo.DEMO_CUSTOMER_ID,
            "occasion": demo.DEMO_OCCASION,
        },
    )

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload is not None

    # A real recommendation object came back (ok true), not a fabricated placeholder.
    assert "recommendation" in payload
    recommendation = payload["recommendation"]
    assert recommendation["ok"] is True
    assert recommendation["recommended_size"] is not None
    assert isinstance(recommendation.get("explanation"), str) and recommendation["explanation"].strip()

    # A visible masked trace is attached.
    trace = recommendation.get("trace")
    assert isinstance(trace, dict), "the recommendation must carry a trace dict"
    assert trace.get("steps"), "the masked trace must expose its ordered steps"

    # The trace is a *visible masked trace* (Req 7.5/10.3): ``masked_fields`` is
    # present and names the two redacted direct identifiers, so a consumer can see
    # which identifiers are masked.
    assert "masked_fields" in trace
    assert set(MASKED_FIELDS).issubset(set(trace["masked_fields"]))

    # Masking is genuinely applied, not merely declared: wherever a masked identifier
    # key (``customer_uid`` / ``cnsmr_prof_sid``) appears anywhere in the trace, its
    # value is the ``***MASKED***`` placeholder and never a raw identifier value. The
    # masker only writes the placeholder when a direct-identifier key actually flows
    # through recorded tool data; this check holds whether or not that happens, and
    # fails loudly if any masked key ever carried an unmasked value.
    def _assert_masked(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in MASKED_FIELDS:
                    assert value == MASK_PLACEHOLDER, (
                        f"masked field {key!r} must be redacted to {MASK_PLACEHOLDER!r}, "
                        f"got {value!r}"
                    )
                _assert_masked(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _assert_masked(item)

    _assert_masked(trace)


# ---------------------------------------------------------------------------
# The served page carries the Prototype_Disclaimer + Demo_Guide (Reqs 1.3, 1.4)
# ---------------------------------------------------------------------------


def test_root_serves_the_demo_ui_shell(smoke_client):
    """``GET /`` serves the Demo_UI shell from the single process (Req 9.1, 7.5)."""
    resp = smoke_client.get("/")

    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("Content-Type", "")

    html = resp.get_data(as_text=True)
    assert "<!DOCTYPE html>" in html
    assert "ALDO FIT" in html
    # The shell carries the disclaimer + demo-guide containers app.js fills from config.
    assert 'id="prototype-disclaimer"' in html
    assert 'id="demo-guide"' in html
    assert "/static/app.js" in html


def test_served_page_config_carries_disclaimer_and_demo_guide(smoke_client):
    """The served page's config carries the Prototype_Disclaimer + Demo_Guide (Reqs 1.3, 1.4).

    The disclaimer text and ordered demo steps are served via ``/api/config`` (the
    HTML has placeholders that app.js fills), so "the served page contains the
    Prototype_Disclaimer and Demo_Guide" is asserted against the config the page
    loads: the exact disclaimer text and every ordered Demo_Guide step appear.
    """
    resp = smoke_client.get("/api/config")

    assert resp.status_code == 200
    config = resp.get_json()
    assert config is not None

    # Prototype_Disclaimer (Req 1.3): the exact disclaimer text is served.
    assert config.get("disclaimer") == PROTOTYPE_DISCLAIMER
    assert "prototype" in config["disclaimer"].lower()
    assert "not an ALDO storefront" in config["disclaimer"]

    # Demo_Guide (Req 1.4): the ordered steps are served, in order, complete.
    assert config.get("demo_steps") == list(DEMO_GUIDE_STEPS)
    assert len(config["demo_steps"]) >= 1
    for step in DEMO_GUIDE_STEPS:
        assert step in config["demo_steps"]


# ---------------------------------------------------------------------------
# No Bedrock / network in Stub_Mode (Requirements 8.3, 9.2)
# ---------------------------------------------------------------------------


def test_stub_mode_uses_no_live_bedrock_client(smoke_app, smoke_client):
    """The whole flow runs offline through the stub, never the live Bedrock client (Reqs 8.3, 9.2).

    The exploding ``BotoBedrockConverseClient`` installed by the fixture would raise
    if the live client were ever constructed; the orchestrator's injected client is
    therefore the canned Stub_Mode client, and a full recommendation still succeeds —
    proving the end-to-end flow touches no AWS/network.
    """
    agent = smoke_app.config["AI_FIT_AGENT"]
    injected_client = agent.orchestrator._client

    # The injected client is NOT the live Bedrock client.
    assert not isinstance(injected_client, BotoBedrockConverseClient)

    # And a recommendation drives the loop to completion entirely offline.
    resp = smoke_client.post(
        "/api/recommendation",
        json={
            "sku": demo.DEMO_SKU,
            "size": demo.DEMO_STATED_SIZE,
            "customer_id": demo.DEMO_CUSTOMER_ID,
            "occasion": demo.DEMO_OCCASION,
        },
    )
    assert resp.status_code == 200
    assert resp.get_json()["recommendation"]["ok"] is True
