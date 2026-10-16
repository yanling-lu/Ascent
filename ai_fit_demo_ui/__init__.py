"""AI Fit Demo UI — a browser-based demonstration surface for the AI Fit agent.

This package is a *thin presentation layer* over the existing ``ai_fit`` package.
It adds **no fit logic**: every recommendation, occasion result, alternative list,
and reasoning trace comes from ``ai_fit`` (``ConverseOrchestrator.produce_recommendation``,
``match_occasion``, ``find_alternative_styles``), serialized field-for-field and served
to a static browser page.

The package layering (per the design):

    service.py     the single Flask ``Recommendation_Service`` — loads the agent
                   once at startup, exposes the JSON endpoints, and serves the
                   Demo_UI static assets from one process.
    __main__.py    ``python -m ai_fit_demo_ui`` entry point (``--stub`` / live mode).

Run the demo with a single command::

    python -m ai_fit_demo_ui            # live Bedrock mode (default)
    python -m ai_fit_demo_ui --stub     # offline Stub_Mode (no AWS)
    AI_FIT_DEMO_STUB=1 python -m ai_fit_demo_ui   # offline via env var
"""

from __future__ import annotations

from ai_fit_demo_ui.service import (
    ProductConfig,
    DEFAULT_PRODUCT_CONFIG,
    create_app,
)

__version__ = "0.1.0"

__all__ = [
    "ProductConfig",
    "DEFAULT_PRODUCT_CONFIG",
    "create_app",
]
