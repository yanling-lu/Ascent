"""Startup tests for the Recommendation_Service (Task 1.2).

Covers the two startup guarantees the task calls for, both exercised through
``ai_fit_demo_ui.service.create_app(stub=True, ...)`` and the Flask test client:

* **Single load, reused store (Requirement 7.3)** — the agent / ``DataStore`` is
  built *once* at startup and reused by every request; it is never reloaded per
  request. Asserted two ways: ``DataStore.load`` is called exactly once while
  serving several requests, and ``app.config["AI_FIT_AGENT"]`` (and its ``.store``)
  is the *same object* across those requests (shared object identity).

* **No Bedrock / network in Stub_Mode (Requirements 8.3, 9.2)** — building the app
  in Stub_Mode makes no live Bedrock client and no network call. Asserted by
  replacing ``BotoBedrockConverseClient`` with a sentinel that *raises* if it is ever
  constructed, and by confirming the orchestrator's injected client is the canned
  Stub_Mode client rather than the live Bedrock client.

Performance / hermeticity: the real ``make_orchestrator`` calls
``DataStore.load`` against the ~1M-row production CSVs (~75s cold). To keep this
test fast and fully offline, ``DataStore.load`` is monkeypatched to return a tiny
in-memory store carrying just the demo SKU + customer (mirroring the hermetic store
used by the demo smoke tests). No test here loads the real datasets or touches AWS.
"""

from __future__ import annotations

from ai_fit import demo
from ai_fit.data.store import DataStore
from ai_fit.orchestrator import BotoBedrockConverseClient

from ai_fit_demo_ui import service


# ---------------------------------------------------------------------------
# A tiny, hermetic in-memory store for the demo SKU + customer
# ---------------------------------------------------------------------------


def _build_small_store() -> DataStore:
    """Build a tiny :class:`DataStore` carrying just the demo SKU + customer.

    Mirrors the hermetic store the demo smoke tests use: a handful of raw rows keyed
    by the normalized CSV column names, with the real ``_build_indexes`` run so the
    tool/mapper path works against real logic over a few rows (not the production
    CSVs). The SKU leans "runs small" with ``work`` suitability so the rubric-
    consistent Stub_Mode decision validates, but this test only needs a loadable
    store — it asserts startup wiring, not recommendation content.
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


class _LoadSpy:
    """A ``DataStore.load`` replacement that counts calls and returns a small store.

    Lets a test assert the data store is built exactly once at startup (and never
    reloaded per request) while keeping the load hermetic and instant.
    """

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, dataset_paths=None) -> DataStore:
        self.calls += 1
        return _build_small_store()


class _ExplodingBedrockClient:
    """Stand-in for ``BotoBedrockConverseClient`` that fails if ever constructed.

    In Stub_Mode the service wires the canned stub client and must *never* build the
    live Bedrock client (which would create an AWS/network client). Installing this
    as the ``BotoBedrockConverseClient`` the service sees turns any accidental live
    wiring into a loud failure instead of a silent network call.
    """

    def __init__(self, *args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError(
            "Stub_Mode must not construct a live Bedrock client (no AWS/network)."
        )


# ---------------------------------------------------------------------------
# Single-load / shared-store reuse (Requirement 7.3)
# ---------------------------------------------------------------------------


def test_store_loaded_once_at_startup_not_per_request(monkeypatch):
    """The data store is loaded exactly once at startup, not per request (Req 7.3)."""
    load_spy = _LoadSpy()
    # Patch the name the service's make_orchestrator actually calls.
    monkeypatch.setattr(demo.DataStore, "load", staticmethod(load_spy))

    app = service.create_app(stub=True)

    # Exactly one load happened while building the app...
    assert load_spy.calls == 1

    client = app.test_client()
    # ...and serving several requests triggers no further loads.
    for _ in range(3):
        resp = client.get("/api/config")
        assert resp.status_code == 200

    assert load_spy.calls == 1, "the data store must not be reloaded per request"


def test_agent_and_store_are_shared_across_requests(monkeypatch):
    """``app.config['AI_FIT_AGENT']`` and its store are one shared object (Req 7.3)."""
    monkeypatch.setattr(demo.DataStore, "load", staticmethod(_LoadSpy()))

    app = service.create_app(stub=True)
    agent = app.config["AI_FIT_AGENT"]
    store = agent.store

    captured = []

    # Register a probe route that records the agent/store identity seen per request.
    @app.route("/__probe_agent_identity")
    def _probe():  # type: ignore[unused-ignore]
        a = app.config["AI_FIT_AGENT"]
        captured.append((id(a), id(a.store)))
        return "ok"

    client = app.test_client()
    for _ in range(3):
        assert client.get("/__probe_agent_identity").status_code == 200

    # Every request saw the very same agent + store objects built at startup.
    assert captured, "probe route should have run"
    assert all(seen == (id(agent), id(store)) for seen in captured)
    # Shared object identity, not just equality.
    assert app.config["AI_FIT_AGENT"] is agent
    assert agent.store is store


# ---------------------------------------------------------------------------
# No Bedrock / network call in Stub_Mode (Requirements 8.3, 9.2)
# ---------------------------------------------------------------------------


def test_stub_mode_builds_no_live_bedrock_client(monkeypatch):
    """Building the app in Stub_Mode constructs no live Bedrock client (Req 8.3, 9.2)."""
    monkeypatch.setattr(demo.DataStore, "load", staticmethod(_LoadSpy()))
    # Any attempt to construct the live Bedrock client raises — proving none happens.
    monkeypatch.setattr(demo, "BotoBedrockConverseClient", _ExplodingBedrockClient)

    # Must not raise: Stub_Mode wires the canned stub, never the live client.
    app = service.create_app(stub=True)

    assert app.config["AI_FIT_STUB_MODE"] is True


def test_stub_mode_orchestrator_uses_the_stub_client(monkeypatch):
    """The orchestrator's injected client is the Stub_Mode client, not Bedrock (Req 8.3, 9.2)."""
    monkeypatch.setattr(demo.DataStore, "load", staticmethod(_LoadSpy()))

    app = service.create_app(stub=True)
    agent = app.config["AI_FIT_AGENT"]

    injected_client = agent.orchestrator._client
    # It is NOT the live Bedrock client...
    assert not isinstance(injected_client, BotoBedrockConverseClient)
    # ...and issuing a recommendation request drives the loop entirely offline
    # through that stub client (no AWS), returning a serialized recommendation.
    client = app.test_client()
    resp = client.post(
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
    assert "recommendation" in payload
    assert payload["recommendation"]["ok"] is True
