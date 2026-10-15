"""Reasoning layer: occasion normalization, pure sizing helpers, and the
deterministic Guardrail Validator.

The Guardrail Validator is a pure function over (proposed_decision, signals) with
no I/O and no randomness; it enforces the design's hard invariants as a backstop
behind Claude's rubric-guided reasoning.
"""
