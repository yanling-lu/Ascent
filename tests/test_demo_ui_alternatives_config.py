"""Endpoint tests for ``/api/alternatives`` and ``/api/config`` (Task 3.3).

Covers the two things the task calls for, run entirely in Stub_Mode against the
Flask test client with **no AWS** and **no production-CSV load** (Requirement 8.3):

* ``POST /api/alternatives`` returns the ``find_alternative_styles`` dict unchanged
  (``requested_occasion``, ``alternatives[]``, ``none_available``, ``message``), and
  the none-available case returns ``none_available: true`` (Requirements 5.1, 5.3).
* ``GET /api/config`` carries **only** ``usual_shoe_size`` + ``comfort_style_priority``
  for the customer summary and contains **no** Protected_Attribute key at any nesting
  depth (Requirements 1.5, 2.2).

Hermeticity: :func:`ai_fit_demo_ui.service.create_app` wires the agent via
``make_orchestrator`` which loads the ~1M-row production CSVs (~90s). To keep this
test fast and fully offline we build a **tiny in-memory** :class:`DataStore` (a
handful of rows for the demo SKU + customer, plus one alternative SKU that suits a
different occasion so the none-available branch is reachable) and wire the Flask app
over it directly — mirroring ``create_app``'s route registration without the heavy
load. No test here touches Bedrock or the real datasets.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from flask import Flask

from ai_fit import demo
from ai_fit.data.store import DataStore
from ai_fit.orchestrator import ConverseOrchestrator
from ai_fit.tools.human_routing import AuditLog
from ai_fit.tools.outcomes import OutcomeStore
from ai_fit_demo_ui import service


# ---------------------------------------------------------------------------
# Protected_Attribute recursive scanner (Requirements 1.5, 2.2)
# ---------------------------------------------------------------------------

# The Protected_Attribute set defined by the ai-fit spec. None of these may appear
# as a key anywhere in a Demo_UI payload, at any nesting depth.
PROTECTED_KEYS = {"age", "gender", "ethnicity", "nationality", "location"}


def find_protected_keys(obj: Any, path: str = "$") -> List[str]:
    """Recursively collect any Protected_Attribute keys found anywhere in ``obj``.

    Walks dicts and lists to arbitrary depth and returns the JSON path of every key
    (case-insensitively) matching a Protected_Attribute. An empty list means the
    payload is Protected_Attribute-free at every nesting level (Requirements 1.5, 2.2).
    """
    hits: List[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and key.strip().lower() in PROTECTED_KEYS:
                hits.append(f"{path}.{key}")
            hits.extend(find_protected_keys(value, f"{path}.{key}"))
    elif isinstance(obj, (list, tuple)):
        for i, value in enumerate(obj):
            hits.extend(find_protected_keys(value, f"{path}[{i}]"))
    return hits


# ---------------------------------------------------------------------------
# A tiny, hermetic in-memory store for the demo SKU + customer + one alternative
# ---------------------------------------------------------------------------


def _build_small_store() -> DataStore:
    """Build a tiny :class:`DataStore` for the demo SKU/customer plus one alternative.

    * The demo customer (``ALDO_SYN_0005``) shops for ``work`` with a usual size 7.5
      and a ``comfort`` comfort-style priority — so ``/api/config`` has a real,
      non-empty customer summary to forward.
    * The browsed demo SKU (``15887957``) is suited for ``work`` (a single catalog
      row, so it has no same-style sibling SKUs that would themselves count as
      alternatives).
    * A second catalog SKU (``ALT_PARTY``) is suited only for ``special occasion`` —
      so ``find_alternative_styles(store, "work", demo_sku)`` finds **no** candidate
      (the browsed SKU is excluded and the only other SKU does not suit ``work``),
      exercising the ``none_available: true`` branch (Requirement 5.3). Querying the
      ``special occasion`` occasion instead surfaces that SKU as an alternative,
      exercising the populated-dict branch (Requirement 5.1).
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

    # Browsed SKU: a single catalog row suited for work. Keeping it to one row (one
    # catalog SKU key) means there are no same-style sibling SKUs that would count as
    # their own alternatives, so the only alternative candidate is ALT_PARTY below.
    catalog_rows: List[Dict[str, str]] = [
        {
            "sku": sku,
            "style": "DEMOSTYLE",
            "size": str(demo.DEMO_STATED_SIZE),
            "category": "heels",
            "price_usd": "79.99",
            "occasions": "work|casual",
            "fit_guidance": "Runs small - consider sizing up from your usual size",
            "fit_return_rate": "0.08",
            "toe_box": "regular",
            "gender_category": "women",
        }
    ]

    # One alternative SKU suited only for "special occasion" (NOT work), so the
    # none-available branch is reachable for the demo "work" occasion and the
    # populated branch is reachable for the "special occasion" occasion.
    catalog_rows.append(
        {
            "sku": "ALT_PARTY",
            "style": "PARTYSTYLE",
            "size": "7.5",
            "category": "heels",
            "price_usd": "99.99",
            # "night out" normalizes to the canonical "special occasion" occasion
            # (the catalog vocabulary has no literal "special occasion" token).
            "occasions": "night out",
            "fit_guidance": "True to size",
            "fit_return_rate": "0.05",
            "toe_box": "regular",
            "gender_category": "women",
        }
    )

    oms_returns_rows = [
        {"sku": sku, "order_number": f"ORD{i}", "reason": "Too small"} for i in range(12)
    ] + [{"sku": sku, "order_number": "ORDQ", "reason": "Defective"}]
    oms_orders_rows = [
        {"sku": sku, "order_number": f"ORD{i}", "shipped": "10"} for i in range(5)
    ]
    review_rows = [
        {"sku": sku, "fit_feedback": "runs_small", "width_feedback": "normal", "occasion": "work"},
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


def _build_app_over_store(store: DataStore) -> Flask:
    """Wire the Demo_UI Flask app over an injected small store (Stub_Mode, no AWS).

    Mirrors :func:`ai_fit_demo_ui.service.create_app` exactly — the same resolved
    :class:`ProductConfig`, the same stored agent, and the same route registration —
    but builds the :class:`~ai_fit.demo.DemoAgent` over the injected tiny store so no
    production CSV is ever loaded. The orchestrator is the offline stub client, so
    building and calling the app performs no Bedrock/network call (Requirement 8.3).
    """
    outcome_store = OutcomeStore()
    audit_log = AuditLog()
    tool_specs = demo.build_tool_specs(store, outcome_store, audit_log)
    orchestrator = ConverseOrchestrator(demo._default_stub(), tool_specs)
    agent = demo.DemoAgent(store=store, orchestrator=orchestrator, tool_specs=tool_specs)

    product_config = service.DEFAULT_PRODUCT_CONFIG
    offered_sizes = service._resolve_offered_sizes(agent, product_config)
    resolved_config = service.ProductConfig(
        sku=product_config.sku,
        name=product_config.name,
        price_usd=product_config.price_usd,
        color_options=list(product_config.color_options),
        image_path=product_config.image_path,
        offered_sizes=offered_sizes,
        demo_occasion=product_config.demo_occasion,
        demo_customer_id=product_config.demo_customer_id,
    )

    app = Flask(service.__name__)
    app.config["AI_FIT_AGENT"] = agent
    app.config["AI_FIT_PRODUCT_CONFIG"] = resolved_config
    app.config["AI_FIT_STUB_MODE"] = True

    service._register_recommendation_route(app)
    service._register_alternatives_route(app)
    service._register_config_route(app)
    service._register_static_routes(app)
    service._register_error_handler(app)
    return app


@pytest.fixture
def client():
    """A Flask test client over the hermetic small-store app (Stub_Mode)."""
    app = _build_app_over_store(_build_small_store())
    app.testing = True
    return app.test_client()


# ---------------------------------------------------------------------------
# POST /api/alternatives (Requirements 5.1, 5.3)
# ---------------------------------------------------------------------------


def test_alternatives_returns_find_alternative_styles_dict(client):
    """``/api/alternatives`` returns the ``find_alternative_styles`` dict unchanged (Req 5.1)."""
    resp = client.post(
        "/api/alternatives",
        json={"sku": demo.DEMO_SKU, "occasion": "special occasion"},
    )
    assert resp.status_code == 200
    body = resp.get_json()

    # The dict is returned field-for-field — the exact shape find_alternative_styles
    # produces, with no key renaming or filtering (Requirements 5.1, 7.2).
    assert set(body.keys()) == {
        "requested_occasion",
        "alternatives",
        "none_available",
        "message",
    }
    assert body["requested_occasion"] == "special occasion"
    assert body["none_available"] is False
    # The one alternative SKU that suits "special occasion" is surfaced (<= 3 cap).
    assert isinstance(body["alternatives"], list)
    assert 1 <= len(body["alternatives"]) <= 3
    alt_skus = {alt["sku"] for alt in body["alternatives"]}
    assert "ALT_PARTY" in alt_skus
    # The browsed SKU is never recommended back to the customer.
    assert demo.DEMO_SKU not in alt_skus
    # Each alternative carries the tool's name/price/occasions projection.
    for alt in body["alternatives"]:
        assert set(alt.keys()) == {"sku", "category", "price_usd", "occasions"}
        assert "special occasion" in alt["occasions"]


def test_alternatives_none_available_reports_true(client):
    """The none-available case returns ``none_available: true`` (Req 5.3).

    For the demo ``work`` occasion the only non-browsed catalog SKU (``ALT_PARTY``)
    suits ``special occasion`` only, so no alternative matches and the tool reports
    the explicit none-available result.
    """
    resp = client.post(
        "/api/alternatives",
        json={"sku": demo.DEMO_SKU, "occasion": demo.DEMO_OCCASION},
    )
    assert resp.status_code == 200
    body = resp.get_json()

    assert body["none_available"] is True
    assert body["alternatives"] == []
    assert body["requested_occasion"] == demo.DEMO_OCCASION
    # A customer-facing message explains none is currently available.
    assert isinstance(body["message"], str) and body["message"].strip()


def test_alternatives_payload_has_no_protected_attribute(client):
    """The alternatives payload carries no Protected_Attribute at any depth (Reqs 1.5, 2.2)."""
    resp = client.post(
        "/api/alternatives",
        json={"sku": demo.DEMO_SKU, "occasion": "special occasion"},
    )
    assert resp.status_code == 200
    assert find_protected_keys(resp.get_json()) == []


# ---------------------------------------------------------------------------
# GET /api/config (Requirements 1.5, 2.2)
# ---------------------------------------------------------------------------


def test_config_customer_summary_has_only_two_whitelisted_fields(client):
    """``/api/config`` customer summary carries only usual size + comfort priority (Reqs 1.5, 2.2)."""
    resp = client.get("/api/config")
    assert resp.status_code == 200
    body = resp.get_json()

    assert "customer_summary" in body
    summary = body["customer_summary"]
    # EXACTLY the two whitelisted keys — nothing else leaks through.
    assert set(summary.keys()) == {"usual_shoe_size", "comfort_style_priority"}
    # The values are the real ones derived from get_customer_profile.
    assert summary["usual_shoe_size"] == str(demo.DEMO_STATED_SIZE)
    assert summary["comfort_style_priority"] == "comfort"


def test_config_payload_has_no_protected_attribute_at_any_depth(client):
    """The whole ``/api/config`` payload contains no Protected_Attribute key (Reqs 1.5, 2.2)."""
    resp = client.get("/api/config")
    assert resp.status_code == 200
    body = resp.get_json()

    hits = find_protected_keys(body)
    assert hits == [], f"protected keys leaked into /api/config: {hits}"


def test_config_customer_summary_omits_fit_profile_and_occasions(client):
    """The summary drops the richer profile fields (fit_profile, occasions, sizing).

    ``get_customer_profile`` returns more than the two whitelisted fields; the config
    endpoint must forward only ``usual_shoe_size`` and ``comfort_style_priority`` and
    drop everything else (Requirements 1.5, 2.2).
    """
    resp = client.get("/api/config")
    body = resp.get_json()
    summary = body["customer_summary"]

    for dropped in ("fit_profile", "occasions", "sizing_system", "found"):
        assert dropped not in summary


# ---------------------------------------------------------------------------
# Protected-key scanner self-check (guards the assertion itself)
# ---------------------------------------------------------------------------


def test_protected_key_scanner_detects_nested_keys():
    """The recursive scanner catches a protected key nested at depth (meta-test)."""
    payload = {
        "product": {"name": "x"},
        "customer_summary": {"usual_shoe_size": "7.5"},
        "nested": [{"deep": {"gender": "redacted-should-not-happen"}}],
    }
    hits = find_protected_keys(payload)
    assert hits == ["$.nested[0].deep.gender"]

    assert find_protected_keys({"customer_summary": {"usual_shoe_size": "7.5"}}) == []


# ---------------------------------------------------------------------------
# Demo occasion scoping (_scope_occasion_match) — demo-only presentation layer
# ---------------------------------------------------------------------------


def test_scope_occasion_match_keeps_only_product_occasions():
    """The demo scopes a broad agent match down to the product's occasions.

    The underlying data SKU may be tagged for several occasions, but the demo
    presents the Stessy2.0 as a special-occasion heel. ``_scope_occasion_match``
    intersects the agent's match against ``product_occasions`` and recomputes the
    status/message. This is a DEMO presentation step only — it never changes the
    agent's size/fit reasoning.
    """
    agent_match = {
        "status": "match",
        "matched_occasions": ["work", "everyday wear", "special occasion"],
        "customer_occasions": ["special occasion"],
        "sku_occasions": ["work", "everyday wear", "special occasion"],
        "review_cues_used": [],
        "unrecognized_values": [],
        "message": "This product matches your occasion: work.",
    }

    # Asking about "special occasion" -> stays a match, scoped to that occasion.
    scoped = service._scope_occasion_match(agent_match, ["special occasion"])
    assert scoped["status"] == "match"
    assert scoped["matched_occasions"] == ["special occasion"]
    assert scoped["sku_occasions"] == ["special occasion"]
    assert "special occasion" in scoped["message"]


def test_scope_occasion_match_flips_non_product_occasion_to_no_match():
    """An everyday-wear match on a special-occasion product becomes no_match."""
    agent_match = {
        "status": "match",
        "matched_occasions": ["everyday wear"],
        "customer_occasions": ["everyday wear"],
        "sku_occasions": ["work", "everyday wear", "special occasion"],
        "review_cues_used": [],
        "unrecognized_values": [],
        "message": "This product matches your occasion: everyday wear.",
    }
    scoped = service._scope_occasion_match(agent_match, ["special occasion"])
    assert scoped["status"] == "no_match"
    # The customer asked for everyday wear, which the special-occasion product
    # does not match -> no matched occasions.
    assert scoped["matched_occasions"] == []
    # The product's own suited occasions are still scoped to the whitelist.
    assert scoped["sku_occasions"] == ["special occasion"]


def test_scope_occasion_match_passes_through_non_resolved_statuses():
    """Non match/no_match statuses pass through unchanged (needs_occasion, etc.)."""
    for status in ("needs_occasion", "sku_not_found", "occasion_unavailable"):
        src = {
            "status": status,
            "matched_occasions": [],
            "customer_occasions": [],
            "sku_occasions": [],
            "review_cues_used": [],
            "unrecognized_values": [],
            "message": "unchanged",
        }
        out = service._scope_occasion_match(src, ["special occasion"])
        assert out == src
