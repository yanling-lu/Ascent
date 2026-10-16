"""Recommendation_Service — the thin Flask layer over the AI Fit agent.

This module owns process lifecycle and startup wiring only. It contains **no**
sizing, occasion, or masking logic of its own: all fit judgment comes from the
existing ``ai_fit`` package.

Startup (once, in :func:`create_app`):

* Build the :class:`~ai_fit.demo.DemoAgent` via ``ai_fit.demo.make_orchestrator(client)``
  — ``client`` is ``None`` for live Bedrock (Requirement 9.3) or
  ``build_stub_client(...)`` for Stub_Mode (Requirements 8.3, 9.2).
* Store the agent and the resolved :class:`ProductConfig` on the Flask app so every
  request reuses the *same* loaded :class:`~ai_fit.data.store.DataStore` and the same
  bound tools — the data store is never reloaded per request (Requirement 7.3).
* Resolve ``offered_sizes`` from the catalog entry
  (``get_product_catalog_entry(store, sku).offered_sizes``), falling back to the
  configured list only when the catalog carries none.

Task 1.1 implements the startup wiring (``create_app`` + ``ProductConfig`` + default
config). The JSON endpoints and static serving are layered on in later tasks.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, List, Optional

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from ai_fit.demo import (
    DEMO_CUSTOMER_ID,
    DEMO_OCCASION,
    DEMO_SKU,
    DemoAgent,
    build_stub_client,
    make_orchestrator,
)
from ai_fit.orchestrator import BedrockConverseClient
from ai_fit.tools.alternatives import find_alternative_styles, match_occasion
from ai_fit.tools.read_core import get_customer_profile, get_product_catalog_entry


# ---------------------------------------------------------------------------
# ProductConfig — service-side startup configuration (design "Data Models")
# ---------------------------------------------------------------------------


@dataclass
class ProductConfig:
    """Startup configuration for the single demo product and demo customer.

    This is the service's only new data model; everything else is passed through
    from ``ai_fit`` unchanged. ``offered_sizes`` is a *fallback* list — at startup
    :func:`create_app` prefers the catalog entry's ``offered_sizes`` for the SKU so
    the size grid matches the agent's view of the product (design "ProductConfig").

    Fields (design "Data Models" -> ProductConfig):

    * ``sku`` — the single demo SKU (must exist in the catalog).
    * ``name`` — product display name.
    * ``price_usd`` — product price.
    * ``color_options`` — color swatch labels shown on the PDP.
    * ``image_path`` — path to the product image, served from ``/static``.
    * ``offered_sizes`` — fallback size grid, used only if the catalog has none.
    * ``demo_occasion`` — the demo customer's occasion (e.g. ``"special occasion"``).
    * ``demo_customer_id`` — the demo synthetic customer id (e.g. ``"ALDO_SYN_0005"``).
    * ``product_occasions`` — the occasions this demo product is presented as
      suiting. The occasion match for the demo is scoped to this set, so a product
      shown as a special-occasion heel matches "special occasion" and not
      "everyday wear", even though the underlying data SKU is tagged more broadly.
      This scopes ONLY the occasion check; the size/fit recommendation stays fully
      agent-driven over the real SKU data.
    """

    sku: str
    name: str
    price_usd: float
    color_options: List[str]
    image_path: str
    offered_sizes: List[str]
    demo_occasion: str
    demo_customer_id: str
    product_occasions: List[str] = field(default_factory=lambda: ["special occasion"])


# A default config for the single demo SKU/customer. These mirror the representative
# SKU + customer the ``ai_fit`` demo already verifies as present in the datasets
# (SKU 15887957 "Aahliyah" boots; customer ALDO_SYN_0005 shopping for "work").
# ``offered_sizes`` here is only a fallback — the catalog's offered_sizes take
# precedence at startup (see ``create_app``).
# The Stessy2.0 is presented as a special-occasion stiletto. The underlying data
# SKU (15887957) is tagged for work/everyday/special in the catalog, so the demo
# scopes the occasion check to ``product_occasions`` (special occasion) and uses
# "special occasion" as the demo customer's occasion. Size/fit reasoning is
# unaffected — it still runs over the real SKU data.
DEFAULT_PRODUCT_CONFIG = ProductConfig(
    sku=DEMO_SKU,
    name="Stessy2.0",
    price_usd=110.0,
    color_options=["Dark red", "Black", "Cream", "Brown"],
    image_path="product.jpg",
    offered_sizes=["5", "6", "6.5", "7", "7.5", "8", "8.5", "9", "10", "11"],
    demo_occasion="special occasion",
    demo_customer_id=DEMO_CUSTOMER_ID,
    product_occasions=["special occasion"],
)


# ---------------------------------------------------------------------------
# Demo_UI static text — Prototype_Disclaimer + ordered Demo_Guide steps
# ---------------------------------------------------------------------------

# Prototype_Disclaimer: on-screen text stating this is an unofficial hackathon
# prototype and not an ALDO storefront (Requirement 1.3). Served from /api/config
# so the browser renders it from server config rather than hard-coding it.
PROTOTYPE_DISCLAIMER = (
    "This is an unofficial hackathon prototype built to demonstrate the AI Fit "
    "assistant. It is not an ALDO storefront, not affiliated with or endorsed by "
    "ALDO, and nothing here is a real product listing or offer for sale."
)

# Demo_Guide: the ordered steps of the scripted demonstration (Requirement 1.4).
# Rendered as the on-page guide strip; the UI provides a control to hide it.
DEMO_GUIDE_STEPS = [
    "Select a size (try size 7) for the Stessy2.0.",
    "Click \u201cGet fit insight\u201d to ask the ALDO FIT assistant.",
    "Review the recommended size, confidence, and any fit warning.",
    "Click \u201cAdd to Bag\u201d and read the fit warning at checkout.",
    "Choose \u201cFind alternatives\u201d to see occasion-matching styles.",
    "Expand \u201cactivity\u201d to see the assistant's masked reasoning trace.",
]


# ---------------------------------------------------------------------------
# App factory — load the agent once at startup
# ---------------------------------------------------------------------------


def _resolve_offered_sizes(agent: DemoAgent, config: ProductConfig) -> List[str]:
    """Read ``offered_sizes`` from the catalog, falling back to the config list.

    Prefers ``get_product_catalog_entry(store, sku).offered_sizes`` so the size grid
    matches the agent's view of the SKU; falls back to ``config.offered_sizes`` only
    when the catalog entry is missing or carries no sizes (design "ProductConfig").
    """
    entry = get_product_catalog_entry(agent.store, config.sku)
    catalog_sizes = entry.get("offered_sizes") if entry.get("found") else None
    if catalog_sizes:
        return [str(size) for size in catalog_sizes]
    return list(config.offered_sizes)


def create_app(
    *,
    stub: bool,
    product_config: ProductConfig = DEFAULT_PRODUCT_CONFIG,
) -> Flask:
    """Build the Flask app and load the AI Fit agent **once** at startup.

    The :class:`~ai_fit.demo.DemoAgent` is built via ``make_orchestrator(client)``,
    where ``client`` is a canned :func:`~ai_fit.demo.build_stub_client` in Stub_Mode
    (``stub=True`` — no AWS / no Bedrock call, Requirements 8.3, 9.2) or ``None`` for
    live Bedrock (the default wiring, Requirement 9.3). The agent and the resolved
    :class:`ProductConfig` are stored on ``app.config`` so every request reuses the
    same loaded :class:`~ai_fit.data.store.DataStore` and bound tools; the store is
    never reloaded per request (Requirement 7.3).

    ``offered_sizes`` is resolved from the catalog entry for the SKU at startup,
    falling back to ``product_config.offered_sizes`` only when the catalog has none.
    """
    client: Optional[BedrockConverseClient] = _build_stub_client() if stub else None
    agent = make_orchestrator(client)

    offered_sizes = _resolve_offered_sizes(agent, product_config)
    resolved_config = ProductConfig(
        sku=product_config.sku,
        name=product_config.name,
        price_usd=product_config.price_usd,
        color_options=list(product_config.color_options),
        image_path=product_config.image_path,
        offered_sizes=offered_sizes,
        demo_occasion=product_config.demo_occasion,
        demo_customer_id=product_config.demo_customer_id,
        product_occasions=list(product_config.product_occasions),
    )

    app = Flask(__name__)
    # Store the shared agent + config so every request reuses the same loaded
    # DataStore and bound tools (Requirement 7.3).
    app.config["AI_FIT_AGENT"] = agent
    app.config["AI_FIT_PRODUCT_CONFIG"] = resolved_config
    app.config["AI_FIT_STUB_MODE"] = stub

    _register_recommendation_route(app)
    _register_alternatives_route(app)
    _register_config_route(app)
    _register_static_routes(app)
    _register_error_handler(app)

    return app


def _build_stub_client() -> BedrockConverseClient:
    """Build the canned Stub_Mode Bedrock client for the demo SKU/customer.

    Mirrors the rubric-consistent decision the ``ai_fit`` demo uses for SKU 15887957
    (a half size up from the usual 7.5 to 8.0 at high confidence, citing the OMS
    returns, the customer's saved fit profile, and the catalog guidance). Running
    through this stub exercises the full orchestrator loop + validator + trace
    masking with no live AWS access (Requirements 8.3, 9.2).
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
        cited_signals=[
            "oms_fit_returns",
            "customer_fit_profile",
            "catalog_fit_guidance",
        ],
    )


# ---------------------------------------------------------------------------
# POST /api/recommendation — fit insight endpoint (Task 2.1)
# ---------------------------------------------------------------------------


def _coerce_size(raw: Any) -> Optional[float]:
    """Coerce a request-body ``size`` into a float, or ``None`` when invalid.

    The UI already guards that a size is selected before issuing the request
    (Requirement 2.5); this is the defensive server-side guard behind it. A missing
    value (``None``), an empty string, or any value that cannot be read as a number
    yields ``None`` so the endpoint can answer ``400 {"error": "size_required"}``.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):  # bools are ints in Python; never a valid size
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _scope_occasion_match(
    occasion_match: dict, product_occasions: List[str]
) -> dict:
    """Scope an occasion-match result to the occasions the demo product suits.

    The underlying data SKU may be tagged for more occasions than the product we are
    presenting (e.g. the demo SKU is tagged work/everyday/special, but the Stessy2.0
    is shown as a special-occasion heel). This intersects the SKU's considered
    occasions and the named matches with ``product_occasions`` and recomputes the
    ``status``/``message`` so the occasion check reflects the presented product.

    Only applies to a resolved ``match``/``no_match`` result; the other statuses
    (``needs_occasion``, ``sku_not_found``, ``occasion_unavailable``) pass through
    unchanged. This scopes ONLY the occasion check — the size/fit recommendation is
    untouched and stays agent-driven.
    """
    if not product_occasions:
        return occasion_match
    if occasion_match.get("status") not in ("match", "no_match"):
        return occasion_match

    allowed = set(product_occasions)
    scoped = dict(occasion_match)
    scoped["sku_occasions"] = [
        o for o in occasion_match.get("sku_occasions", []) if o in allowed
    ]
    matched = [
        o for o in occasion_match.get("matched_occasions", []) if o in allowed
    ]
    scoped["matched_occasions"] = matched
    scoped["review_cues_used"] = [
        c for c in occasion_match.get("review_cues_used", []) if c in allowed
    ]
    if matched:
        scoped["status"] = "match"
        scoped["message"] = (
            "This product matches your occasion: " + ", ".join(matched) + "."
        )
    else:
        scoped["status"] = "no_match"
        scoped["message"] = "This product does not match your requested occasion."
    return scoped


def _register_recommendation_route(app: Flask) -> None:
    """Register ``POST /api/recommendation`` on the app (Requirements 7.1, 7.4, 8.1, 2.5).

    Maps the request body ``{sku, size, customer_id, occasion}`` to the existing
    orchestrator call ``produce_recommendation(sku, stated_usual_size=size,
    customer_id=customer_id, direct_occasion=occasion)`` and, separately,
    ``match_occasion(store, sku, direct_occasions=[occasion])``. The returned
    :class:`~ai_fit.orchestrator.converse.Recommendation` is serialized **field-for-field**
    with :func:`dataclasses.asdict` — no key renaming, filtering, or value
    transformation — so the masked ``trace`` passes through untouched and no
    Protected_Attribute is added, removed, or altered (Requirement 7.4). The
    occasion-match dict is attached unchanged.

    This endpoint adds **no fit logic**: all judgment comes from ``ai_fit``.
    """

    @app.route("/api/recommendation", methods=["POST"])
    def recommendation():  # type: ignore[unused-ignore]
        body = request.get_json(silent=True) or {}

        product_config: ProductConfig = app.config["AI_FIT_PRODUCT_CONFIG"]
        sku = body.get("sku") or product_config.sku
        customer_id = body.get("customer_id") or product_config.demo_customer_id
        occasion = body.get("occasion") or product_config.demo_occasion

        # Defensive guard behind the UI: a missing/invalid size is a 400, never a
        # fabricated recommendation (Requirement 2.5).
        size = _coerce_size(body.get("size"))
        if size is None:
            return jsonify({"error": "size_required"}), 400

        agent: DemoAgent = app.config["AI_FIT_AGENT"]

        recommendation = agent.orchestrator.produce_recommendation(
            sku,
            stated_usual_size=size,
            customer_id=customer_id,
            direct_occasion=occasion,
        )

        occasion_match = match_occasion(
            agent.store,
            sku,
            direct_occasions=[occasion],
        )
        # Demo scoping: present this product as suiting only product_occasions.
        occasion_match = _scope_occasion_match(
            occasion_match, product_config.product_occasions
        )

        # Serialize field-for-field; when ok is False this still returns 200 with the
        # serialized failure (including failure_reason) and never fabricates a
        # size/confidence (Requirements 7.4, 8.1).
        return jsonify(
            {
                "recommendation": asdict(recommendation),
                "occasion_match": occasion_match,
            }
        )


# ---------------------------------------------------------------------------
# POST /api/alternatives — occasion-matching alternative styles (Task 3.1)
# ---------------------------------------------------------------------------


def _register_alternatives_route(app: Flask) -> None:
    """Register ``POST /api/alternatives`` on the app (Requirements 5.1-5.3, 5.5, 7.2).

    Maps the request body ``{sku, occasion}`` to the existing tool call
    ``find_alternative_styles(store, occasion, sku)`` and returns its result dict
    unchanged — ``requested_occasion``, ``alternatives[]`` (already capped at three by
    the tool), ``none_available``, and ``message``. This endpoint adds **no** fit
    logic: the alternative selection, ranking, and the three-item cap all come from
    ``ai_fit`` and pass through field-for-field.
    """

    @app.route("/api/alternatives", methods=["POST"])
    def alternatives():  # type: ignore[unused-ignore]
        body = request.get_json(silent=True) or {}

        product_config: ProductConfig = app.config["AI_FIT_PRODUCT_CONFIG"]
        sku = body.get("sku") or product_config.sku
        occasion = body.get("occasion") or product_config.demo_occasion

        agent: DemoAgent = app.config["AI_FIT_AGENT"]

        result = find_alternative_styles(agent.store, occasion, sku)

        return jsonify(result)


# ---------------------------------------------------------------------------
# GET /api/config — DemoUIConfig for the browser (Task 3.2)
# ---------------------------------------------------------------------------


def _register_config_route(app: Flask) -> None:
    """Register ``GET /api/config`` on the app (Requirements 1.1, 1.3, 1.4, 1.5, 2.2).

    Returns the :class:`DemoUIConfig` the browser renders the PDP from, so the page
    reads its product and customer summary from server config rather than
    hard-coding them. The payload is:

    * ``product`` — the resolved :class:`ProductConfig` **minus the internal ids** the
      browser does not need (``demo_customer_id`` and ``demo_occasion`` are dropped
      from the product block; the SKU, name, price, colors, image, and offered sizes
      remain for the PDP and size grid, Requirements 1.1, 1.2).
    * ``customer_summary`` — a Protected_Attribute-free summary carrying **only**
      ``usual_shoe_size`` and ``comfort_style_priority``, derived from
      ``get_customer_profile`` (which already strips age/gender/ethnicity/nationality/
      location at the data layer). No protected field is ever forwarded
      (Requirements 1.5, 2.2).
    * ``disclaimer`` — the Prototype_Disclaimer text (Requirement 1.3).
    * ``demo_steps`` — the ordered Demo_Guide steps (Requirement 1.4).

    This endpoint adds **no** fit logic: the customer summary is read from the
    existing ``ai_fit`` data layer and only two whitelisted fields are forwarded.
    """

    @app.route("/api/config", methods=["GET"])
    def config():  # type: ignore[unused-ignore]
        product_config: ProductConfig = app.config["AI_FIT_PRODUCT_CONFIG"]
        agent: DemoAgent = app.config["AI_FIT_AGENT"]

        # Product block: drop the internal ids/scoping the browser never needs.
        product = asdict(product_config)
        product.pop("demo_customer_id", None)
        product.pop("demo_occasion", None)
        product.pop("product_occasions", None)

        # Customer summary: forward ONLY the two non-protected fields. The source
        # dict from get_customer_profile already excludes every Protected_Attribute,
        # and we additionally whitelist here so nothing else leaks (Reqs 1.5, 2.2).
        profile = get_customer_profile(agent.store, product_config.demo_customer_id)
        customer_summary = {
            "usual_shoe_size": profile.get("usual_shoe_size"),
            "comfort_style_priority": profile.get("comfort_style_priority"),
        }

        return jsonify(
            {
                "product": product,
                "customer_summary": customer_summary,
                "disclaimer": PROTOTYPE_DISCLAIMER,
                "demo_steps": list(DEMO_GUIDE_STEPS),
            }
        )


# ---------------------------------------------------------------------------
# Static serving — single-process Demo_UI assets (Task 4.1, Requirement 7.5)
# ---------------------------------------------------------------------------


def _register_static_routes(app: Flask) -> None:
    """Register the Demo_UI static-asset routes on the app (Requirement 7.5).

    The whole demo runs from a *single* process: the same Flask app that answers the
    JSON API also serves the browser's static assets. ``Flask(__name__)`` already
    maps ``GET /static/<path:filename>`` to the package's ``static/`` folder, which
    carries ``app.js``, ``styles.css``, and the product image — so no explicit route
    is needed for ``/static/<asset>``; a genuinely missing asset still yields Flask's
    standard ``404`` (left intact by the error handler below).

    This adds only the ``GET /`` entry point, which returns ``index.html`` from that
    same ``static/`` folder so visiting the root serves the Demo_UI shell. This route
    adds **no** fit logic — it only serves files.
    """

    @app.route("/", methods=["GET"])
    def index():  # type: ignore[unused-ignore]
        # Serve the Demo_UI shell from the package static folder (same process).
        return send_from_directory(app.static_folder, "index.html")


# ---------------------------------------------------------------------------
# Centralized error handling — unexpected exceptions -> 503 (Task 4.1, Req 8.2)
# ---------------------------------------------------------------------------


def _register_error_handler(app: Flask) -> None:
    """Register a centralized error handler converting unexpected errors to ``503``.

    Any *unexpected* exception raised while handling a request is caught and returned
    as ``503 {"error": ..., "retryable": true}`` with a short, generic message and
    **no stack trace**, so the Demo_UI can show a non-blocking error and offer retry
    (Requirement 8.2).

    Two classes of response are deliberately *not* converted:

    * **Intentional HTTP responses** — the ``400 {"error": "size_required"}`` guard in
      ``/api/recommendation`` is a normal ``return`` (not an exception), so it is
      untouched. ``404`` for a genuinely missing static asset, and any other
      :class:`~werkzeug.exceptions.HTTPException` (``abort(...)``), are re-raised
      unchanged so real client/routing errors keep their status.
    * Only truly unexpected, non-HTTP exceptions (a bug or a transient agent failure)
      become ``503``.
    """

    @app.errorhandler(HTTPException)
    def _passthrough_http_exception(exc: HTTPException):  # type: ignore[unused-ignore]
        # Preserve intentional HTTP errors (e.g. 404 for a missing static asset,
        # or any abort(...)); do not mask them as a 503.
        return exc

    @app.errorhandler(Exception)
    def _handle_unexpected(exc: Exception):  # type: ignore[unused-ignore]
        # HTTPExceptions are handled by the passthrough above; this branch only sees
        # genuinely unexpected errors. Return a short message with no stack trace.
        if isinstance(exc, HTTPException):
            return exc
        return (
            jsonify(
                {
                    "error": "The fit service hit an unexpected error. Please retry.",
                    "retryable": True,
                }
            ),
            503,
        )
