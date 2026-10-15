"""Orchestrator: the Bedrock Converse tool-use loop.

Runs Converse with the sizing rubric as the system prompt at temperature 0, feeds
tool results back to Claude, receives the structured decision, hands it to the
Guardrail Validator, and enforces the required-tool guard (Requirement 9.7). Applies
Bedrock throttling retry (9.5) and non-throttling failure handling (9.4).

Public re-exports so callers (e.g. the demo entry point) can import the
orchestrator surface from the package rather than reaching into module files.
"""

from ai_fit.orchestrator.converse import (
    BedrockConverseClient,
    BotoBedrockConverseClient,
    ConverseOrchestrator,
    Recommendation,
    ThrottlingError,
    ToolSpec,
    build_tool_config,
    handle_financial_request,
)
from ai_fit.orchestrator.dispatcher import ToolDispatcher

__all__ = [
    "BedrockConverseClient",
    "BotoBedrockConverseClient",
    "ConverseOrchestrator",
    "Recommendation",
    "ThrottlingError",
    "ToolSpec",
    "ToolDispatcher",
    "build_tool_config",
    "handle_financial_request",
]
