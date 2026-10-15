"""Central configuration for the AI Fit agent.

Holds the Bedrock model wiring (Claude Opus model id, region, temperature), the
tool-dispatch timeout/retry constants, and the dataset path constants pointing at
the six CSVs in the workspace root.

These values are referenced by the orchestrator (Requirement 9.1 — the agent is
orchestrated on AWS Bedrock using Claude Opus as the reasoning model) and the tool
dispatcher (Requirement 9.6 — a 10s per-tool timeout and up to 2 retries).

All constants are module-level and immutable by convention; nothing here performs
I/O or has side effects, so the module is safe to import from any layer.
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Bedrock / Claude Opus model wiring (Requirement 9.1)
# ---------------------------------------------------------------------------

# Anthropic Claude Opus on the Bedrock Runtime. The agent reasons through the
# Bedrock Converse API with tool use; this is the model id the orchestrator passes
# to Converse. Override via the AI_FIT_* environment variables for a different
# region, inference profile, or model revision without touching code.
BEDROCK_MODEL_ID: str = "us.anthropic.claude-opus-5-5"

# AWS region hosting the Bedrock Runtime endpoint.
BEDROCK_REGION: str = "us-east-1"

# Temperature 0 for near-deterministic generation. The design relies on this (plus
# the embedded rubric and the deterministic Guardrail Validator) to make identical
# signals produce identical recommendations (Requirement 10.4).
BEDROCK_TEMPERATURE: float = 0.0

# ---------------------------------------------------------------------------
# Tool dispatch: timeout + retry constants (Requirement 9.6, 8.7)
# ---------------------------------------------------------------------------

# Every Agent_Tool call is wrapped with this timeout. A tool that fails to respond
# within the window is treated as a failure and recorded in the Reasoning_Trace
# (Requirements 8.7, 9.6, 7.6).
TOOL_TIMEOUT_SECONDS: float = 10.0

# Maximum number of retries (in addition to the initial attempt) for a failed tool
# call and for Bedrock throttling (Requirements 9.5, 9.6).
TOOL_MAX_RETRIES: int = 2

# ---------------------------------------------------------------------------
# Dataset paths — the six CSVs in the workspace root
# ---------------------------------------------------------------------------

# Workspace root = the directory two levels up from this file (ai_fit/config.py).
WORKSPACE_ROOT: Path = Path(__file__).resolve().parent.parent

# The six datasets the design's data layer loads. Each constant points at the exact
# CSV filename as it exists in the workspace root.

# Customer fit profiles (keyed by synthetic_customer_id).
CUSTOMER_PROFILES_CSV: Path = (
    WORKSPACE_ROOT / "ALDO_FIT_Final_Synthetic_Customer_Profiles.csv"
)

# Product catalog / SKU attributes (keyed by sku).
PRODUCT_CATALOG_CSV: Path = WORKSPACE_ROOT / "Synthetic_product_skus.csv"

# OMS returns, including fit-return and quality-return reasons (keyed by sku).
OMS_RETURNS_CSV: Path = WORKSPACE_ROOT / "OMS Data_Feb24-Jan26_new.csv"

# All OMS orders — the source of Units_Sold (SUM of Shipped on the same OMS sku key).
OMS_ORDERS_CSV: Path = WORKSPACE_ROOT / "All OMS orders_Feb 2024 to Feb 2026.csv"

# In-store return/exchange records, joined to orders on Original Order Number.
INSTORE_EXCHANGES_CSV: Path = WORKSPACE_ROOT / "McGill_Instore_return_exchanges.csv"

# Synthetic customer reviews (fit / width / occasion signals, keyed by sku).
CUSTOMER_REVIEWS_CSV: Path = WORKSPACE_ROOT / "Synthetic_Customer_Reviews.csv"

# Convenience mapping of logical dataset name -> path, for the data layer loader.
DATASET_PATHS: dict[str, Path] = {
    "customer_profiles": CUSTOMER_PROFILES_CSV,
    "product_catalog": PRODUCT_CATALOG_CSV,
    "oms_returns": OMS_RETURNS_CSV,
    "oms_orders": OMS_ORDERS_CSV,
    "instore_exchanges": INSTORE_EXCHANGES_CSV,
    "customer_reviews": CUSTOMER_REVIEWS_CSV,
}


def missing_datasets() -> list[str]:
    """Return the logical names of any configured datasets that are not present.

    Pure helper used by smoke tests and startup checks to fail fast when a CSV the
    agent depends on is missing from the workspace root.
    """
    return [name for name, path in DATASET_PATHS.items() if not path.is_file()]
