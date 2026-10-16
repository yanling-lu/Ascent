"""``python -m ai_fit_demo_ui`` entry point (Requirement 9.1).

Starts the single Flask ``Recommendation_Service`` process that serves both the JSON
API and the static Demo_UI. Stub_Mode (an offline demo with no AWS access) is
selected by the ``--stub`` flag or the ``AI_FIT_DEMO_STUB=1`` environment variable
(Requirements 8.3, 9.2); the default is live Bedrock mode, which uses the existing
``ai_fit`` wiring unchanged (Requirement 9.3).

Usage::

    python -m ai_fit_demo_ui                      # live Bedrock (default)
    python -m ai_fit_demo_ui --stub               # offline Stub_Mode
    AI_FIT_DEMO_STUB=1 python -m ai_fit_demo_ui    # offline via env var
    python -m ai_fit_demo_ui --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional, Sequence

from ai_fit import config
from ai_fit_demo_ui.service import DEFAULT_PRODUCT_CONFIG, create_app


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m ai_fit_demo_ui",
        description="AI Fit Demo UI — a single-process web demo of the AI Fit agent.",
    )
    parser.add_argument(
        "--stub",
        action="store_true",
        help="Run offline with the canned Bedrock stub (no AWS access required).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface to bind (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5000,
        help="Port to listen on (default: 5000).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point: build the app and start the single serving process."""
    args = _parse_args(argv)
    use_stub = args.stub or os.environ.get("AI_FIT_DEMO_STUB") == "1"

    missing = config.missing_datasets()
    if missing:
        print(
            "Cannot start demo — missing dataset CSV(s): "
            + ", ".join(sorted(missing)),
            file=sys.stderr,
        )
        return 2

    app = create_app(stub=use_stub, product_config=DEFAULT_PRODUCT_CONFIG)
    mode = "Stub_Mode (offline)" if use_stub else "live Bedrock"
    print(f"AI Fit Demo UI — {mode} — http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
