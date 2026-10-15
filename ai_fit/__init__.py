"""AI Fit — an agentic AI assistant for ALDO fit and occasion recommendations.

Built end-to-end on AWS Bedrock (Claude Opus via the Bedrock Converse API with
tool use), backed by the six provided CSV datasets. See .kiro/specs/ai-fit for
the requirements and design that drive this package.

Module layering (per the design):
    data/          CSV loader + in-memory store (pure data access)
    reasoning/     occasion normalization, sizing helpers, Guardrail Validator
    tools/         the 10 Agent Tools (data-access + write/action functions)
    orchestrator/  the Bedrock Converse tool-use loop
    trace/         the Reasoning Trace Builder (ordered trace + identifier masking)
"""

__version__ = "0.1.0"
