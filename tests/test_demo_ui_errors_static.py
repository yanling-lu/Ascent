"""Endpoint tests for error handling and single-process static serving (Task 4.2).

Covers the two things the task calls for, per the design's "Example-based endpoint
tests (service, Stub_Mode)":

* **Centralized error handling (Requirement 8.2)** — an endpoint that raises an
  unexpected exception internally is caught and answered as
  ``503 {"error": ..., "retryable": true}`` with a short message and **no stack
  trace** (no traceback text in the body), so the Demo_UI can show a non-blocking
  error and offer retry. Intentional HTTP errors (the ``400`` size guard, a ``404``
  for a missing static asset) are *not* masked as ``503``.
* **Single-process static serving (Requirement 7.5)** — the same Flask process that
  answers the JSON API also serves the Demo_UI: ``GET /`` returns the ``index.html``
  shell and ``GET /static/app.js`` returns the browser script.

Hermeticity: :func:`ai_fit_demo_ui.service.create_app` calls
``ai_fit.demo.make_orchestrator``, which would load the ~1M-row production CSVs
(~90s) and, in live mode, construct a Bedrock client. These tests exercise only the
error handler and static routes — no recommendation is produced — so we
monkeypatch ``make_orchestrator`` to return a :class:`~ai_fit.demo.DemoAgent` wired
over a **tiny in-memory store** (a handful of rows for the demo SKU + customer).
``create_app`` still runs its *real* route registration, static wiring, and error
handler; nothing loads the real datasets and no live AWS is touched.
"""

from __future__ import annotations

import pytest

from ai_fit import demo
from ai_fit.data.store import DataStore
from ai_fit.orchestrator import ConverseOrchestrator
from ai_fit.tools.human_routing import AuditLog
from ai_fit.tools.outcomes import OutcomeStore

import ai_fit_demo_ui.service as service


# ---------------------------------------------------------------------------
# A tiny, hermetic in-memory store + agent for the demo SKU + customer
# ---------------------------------------------------------------------------


def _build_small_store() -> DataStore:
    """Build a tiny :class:`DataStore` carrying just the demo SKU + customer.

    Mirrors the hermetic store used by the end-to-end smoke test: a handful of raw
    rows (keyed by the normalized CSV column names) so ``create_app`` can read the
    catalog's ``offered_sizes`` and the customer profile at startup without loading
    the production CSVs. These tests never produce a recommendation, so the exact
    fit signals do not matter — only that the demo SKU + customer resolve.
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

    store = DataStore(
        customer_profiles_rows=[customer_row],
        product_catalog_rows=catalog_rows,
        oms_returns_rows=[],
        oms_orders_rows=[],
        instore_exchanges_rows=[],
        customer_reviews_rows=[],
    )
    store._build_indexes()
    return store


def _wire_demo_agent(store: DataStore) -> demo.DemoAgent:
    """Wire a :class:`demo.DemoAgent` over a small store + the default stub client.

    Mirrors :func:`ai_fit.demo.make_orchestrator` (same tool specs + stub-driven
    orchestrator) but against the injected small store, so no production CSV is
    loaded and no live AWS is touched.
    """
    tool_specs = demo.build_tool_specs(store, OutcomeStore(), AuditLog())
    orchestrator = ConverseOrchestrator(demo._default_stub(), tool_specs)
    return demo.DemoAgent(store=store, orchestrator=orchestrator, tool_specs=tool_specs)


@pytest.fixture()
def app(monkeypatch):
    """Build the real Flask app over a hermetic agent (no production load, no AWS).

    Monkeypatches ``service.make_orchestrator`` so ``create_app`` wires itself over
    the tiny in-memory store instead of loading the production datasets, while still
    running its real route registration, static serving, and error handler.
    """

    def _fake_make_orchestrator(client=None):
        # client is build_stub_client(...) in stub mode; ignored here since the small
        # agent uses its own default stub. We never produce a recommendation in these
        # tests, so the client is irrelevant.
        return _wire_demo_agent(_build_small_store())

    monkeypatch.setattr(service, "make_orchestrator", _fake_make_orchestrator)

    flask_app = service.create_app(stub=True)
    flask_app.testing = True
    return flask_app


@pytest.fixture()
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Centralized error handling (Requirement 8.2)
# ---------------------------------------------------------------------------


def test_internal_error_returns_503_retryable_without_stack_trace(app, client):
    """An endpoint raising internally yields 503 {retryable:true}, no traceback (Req 8.2).

    A throwaway route is registered that raises an unexpected (non-HTTP) exception;
    the service's centralized error handler must convert it to a ``503`` with a
    short message and ``retryable: true``, and the body must contain no stack-trace
    text (no module path, no "Traceback", no raised message leaked).
    """
    secret_marker = "boom-internal-detail-should-not-leak"

    @app.route("/api/_explode_for_test", methods=["GET"])
    def _explode():  # pragma: no cover - body only reached via the test client
        raise RuntimeError(secret_marker)

    resp = client.get("/api/_explode_for_test")

    assert resp.status_code == 503
    payload = resp.get_json()
    assert payload is not None, "error response must be JSON, not an HTML error page"
    assert payload.get("retryable") is True
    assert isinstance(payload.get("error"), str) and payload["error"].strip()

    # No stack trace / traceback detail leaks to the client.
    body_text = resp.get_data(as_text=True)
    assert secret_marker not in body_text
    assert "Traceback" not in body_text
    assert "RuntimeError" not in body_text
    assert "File \"" not in body_text
    # The payload is exactly the two documented keys — nothing extra leaked.
    assert set(payload.keys()) == {"error", "retryable"}


def test_size_guard_400_is_not_masked_as_503(client):
    """The intentional 400 size guard is a normal return, not swallowed into 503 (Req 8.2)."""
    resp = client.post("/api/recommendation", json={})

    assert resp.status_code == 400
    payload = resp.get_json()
    assert payload == {"error": "size_required"}


def test_missing_static_asset_404_is_not_masked_as_503(client):
    """A genuinely missing static asset keeps its 404, not converted to 503 (Req 8.2)."""
    resp = client.get("/static/does-not-exist.js")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Single-process static serving (Requirement 7.5)
# ---------------------------------------------------------------------------


def test_root_serves_demo_ui_html(client):
    """GET / serves the Demo_UI index.html shell from the single process (Req 7.5)."""
    resp = client.get("/")

    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("Content-Type", "")

    html = resp.get_data(as_text=True)
    # Hallmarks of the Demo_UI shell: the ALDO FIT panel and the app.js include.
    assert "<!DOCTYPE html>" in html
    assert "ALDO FIT" in html
    assert "/static/app.js" in html


def test_static_app_js_is_served(client):
    """GET /static/app.js serves the browser script from the single process (Req 7.5)."""
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    content_type = resp.headers.get("Content-Type", "")
    assert "javascript" in content_type or "text/" in content_type

    body = resp.get_data(as_text=True)
    assert body.strip(), "app.js should be served with non-empty content"
