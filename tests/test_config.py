"""Smoke tests for the config module and package layout (Task 1).

Verifies the Bedrock/Claude Opus model wiring, the tool timeout/retry constants,
and that the six dataset path constants resolve to real files in the workspace
root. Keeping these as plain assertions lets pytest collect and run them without
any external services.
"""

from __future__ import annotations

import importlib

from ai_fit import config


def test_package_imports_cleanly():
    """The top-level package and every submodule import without error."""
    for name in (
        "ai_fit",
        "ai_fit.config",
        "ai_fit.data",
        "ai_fit.reasoning",
        "ai_fit.tools",
        "ai_fit.orchestrator",
        "ai_fit.trace",
    ):
        importlib.import_module(name)


def test_model_is_claude_opus_on_bedrock():
    """Agent is wired to a Claude Opus model id (Requirement 9.1)."""
    assert "claude-opus" in config.BEDROCK_MODEL_ID
    assert config.BEDROCK_REGION


def test_temperature_is_zero():
    """Near-deterministic generation requires temperature 0 (Requirement 10.4)."""
    assert config.BEDROCK_TEMPERATURE == 0.0


def test_tool_timeout_and_retry_constants():
    """10s tool timeout and 2 retries (Requirements 9.6, 9.5, 8.7)."""
    assert config.TOOL_TIMEOUT_SECONDS == 10.0
    assert config.TOOL_MAX_RETRIES == 2


def test_dataset_paths_cover_six_datasets():
    """Exactly the six datasets the design's data layer loads are configured."""
    assert set(config.DATASET_PATHS) == {
        "customer_profiles",
        "product_catalog",
        "oms_returns",
        "oms_orders",
        "instore_exchanges",
        "customer_reviews",
    }


def test_dataset_paths_point_at_real_csvs():
    """Every configured dataset path resolves to a real CSV in the workspace root."""
    assert config.missing_datasets() == []
    for path in config.DATASET_PATHS.values():
        assert path.is_file(), f"missing dataset: {path}"
        assert path.suffix == ".csv"
