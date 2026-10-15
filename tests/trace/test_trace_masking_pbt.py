"""Property-based test for Reasoning_Trace identifier masking (Task 11.2).

Feature: ai-fit, Property 14: Emitted artifacts always mask customer identifiers
(trace portion).

*For any* recommendation produced for any customer, the emitted Reasoning_Trace
SHALL contain no cleartext ``customer_uid`` or ``cnsmr_prof_sid``.

This test exercises the ``ai_fit.trace.builder.ReasoningTraceBuilder`` masking
guarantee (``mask_identifiers`` / ``MASKED_FIELDS`` / ``MASK_PLACEHOLDER``). The
builder masks the value of any ``customer_uid`` / ``cnsmr_prof_sid`` *key* at any
nesting depth in whatever an Agent_Tool recorded. So the property is checked by
generating arbitrary nested dict/list structures in which those two keys carry a
unique, recognizable raw identifier value, recording that structure through the
builder's record_* methods, building the trace, and asserting:

* the raw identifier value never appears anywhere in ``full_trace()`` (walked
  recursively, including the customer-facing explanation string),
* the mask placeholder is present wherever an identifier was redacted, and
* ``masked_fields`` is exactly ``(customer_uid, cnsmr_prof_sid)``.

To make a leak unambiguous, every raw identifier value is drawn from a dedicated
sentinel alphabet (``RAW_<uuid>``) and embedded ONLY as the value of a masked
key. Non-identifier data is drawn from a disjoint alphabet, so any occurrence of a
sentinel in the output is a genuine masking failure rather than coincidental data.

Validates: Requirements 4.7, 7.5, 10.3
"""

from __future__ import annotations

import uuid
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ai_fit.reasoning.validator import Correction
from ai_fit.trace.builder import (
    MASK_PLACEHOLDER,
    MASKED_FIELDS,
    ReasoningTraceBuilder,
)

# ---------------------------------------------------------------------------
# Sentinel raw-identifier values
# ---------------------------------------------------------------------------
#
# Raw identifier values are drawn from this disjoint, recognizable namespace so
# that any appearance in the built trace is unambiguously a leak (and never a
# coincidental collision with ordinary recorded data, which uses a separate
# "safe" alphabet below).
_RAW_PREFIX = "RAW_IDENTIFIER_"


def _raw_identifier() -> str:
    """A unique sentinel value that must never survive into the built trace."""
    return _RAW_PREFIX + uuid.uuid4().hex


# Ordinary (non-identifier) scalar values. Kept disjoint from the raw-identifier
# namespace: safe strings never start with the sentinel prefix, so they cannot be
# confused with a leaked identifier.
_safe_scalars = st.one_of(
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789 -", max_size=12),
    st.integers(min_value=-1000, max_value=1000),
    st.booleans(),
    st.none(),
    st.floats(allow_nan=False, allow_infinity=False, width=32),
)

# Keys for ordinary (non-masked) mapping entries. Explicitly excludes the two
# masked field names so a masked key only ever appears via the dedicated branch
# that attaches a sentinel value to it.
_safe_keys = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=10
).filter(lambda k: k not in MASKED_FIELDS)


def _arbitrary_data() -> st.SearchStrategy[Any]:
    """Arbitrary nested dict/list/tuple/scalar data.

    Any ``customer_uid`` / ``cnsmr_prof_sid`` key that appears carries a fresh
    sentinel raw-identifier value (so the only sentinels in the structure are the
    ones under masked keys). Ordinary entries use the disjoint safe alphabets.
    This strategy may produce a structure with *no* masked key at all; use
    :func:`_data_with_identifier` to guarantee at least one is embedded.
    """

    def extend(children: st.SearchStrategy[Any]) -> st.SearchStrategy[Any]:
        # Masked entries: one or both identifier keys, each carrying a unique
        # sentinel value that must be redacted.
        masked_entries = st.lists(
            st.sampled_from(MASKED_FIELDS), min_size=1, max_size=2, unique=True
        ).map(lambda keys: {k: _raw_identifier() for k in keys})
        # A dict that mixes ordinary entries with masked identifier entries.
        mixed_dicts = st.builds(
            lambda base, masked: {**base, **masked},
            st.dictionaries(_safe_keys, children, max_size=3),
            masked_entries,
        )
        return st.one_of(
            st.dictionaries(_safe_keys, children, max_size=4),  # ordinary dicts
            mixed_dicts,
            st.lists(children, max_size=4),  # lists
            st.lists(children, max_size=4).map(tuple),  # tuples
        )

    return st.recursive(_safe_scalars, extend, max_leaves=25)


def _data_with_identifier() -> st.SearchStrategy[Any]:
    """Arbitrary nested data guaranteed to embed >=1 masked identifier.

    Wraps arbitrary data in a dict/list that always contains at least one
    identifier key (carrying a fresh sentinel), placed at a random depth by
    recursively nesting the arbitrary payload around it.
    """
    masked_entries = st.lists(
        st.sampled_from(MASKED_FIELDS), min_size=1, max_size=2, unique=True
    ).map(lambda keys: {k: _raw_identifier() for k in keys})

    def wrap(payload: Any, masked: dict) -> Any:
        # Nest the guaranteed-masked dict together with arbitrary payload so the
        # identifier can sit at depth, under lists and other dict keys.
        return {"nested": payload, "ids": masked, "list": [payload, masked]}

    return st.builds(wrap, _arbitrary_data(), masked_entries)


def _iter_strings(value: Any):
    """Yield every string found anywhere in a nested structure (recursively)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str):
                yield k
            yield from _iter_strings(v)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item)


def _collect_raw_sentinels(value: Any) -> set[str]:
    """Return the set of sentinel raw-identifier values embedded in ``value``."""
    found: set[str] = set()
    for s in _iter_strings(value):
        if s.startswith(_RAW_PREFIX):
            found.add(s)
    return found


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(
    step_data=_data_with_identifier(),
    tool_data=_data_with_identifier(),
    tool_name=st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=12),
    decision=st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", max_size=20),
    unavailable=st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", min_size=1, max_size=20),
    rationale=st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", max_size=40),
    confidence=st.sampled_from(["", "low", "medium", "high"]),
)
def test_trace_masks_direct_identifiers_at_any_depth(
    step_data: Any,
    tool_data: Any,
    tool_name: str,
    decision: str,
    unavailable: str,
    rationale: str,
    confidence: str,
) -> None:
    """No raw ``customer_uid`` / ``cnsmr_prof_sid`` value survives into the trace.

    The structures recorded via ``record_step``, ``record_tool_invocation``,
    ``record_decision``, and ``record_tool_failure`` embed sentinel identifier
    values under masked keys at random depths. After build, those sentinels must
    be absent everywhere in ``full_trace()``, the placeholder must appear where
    redaction happened, and ``masked_fields`` must be exactly the two identifiers.
    """
    # Sentinels actually embedded by the generators across the recorded inputs.
    embedded = _collect_raw_sentinels(step_data) | _collect_raw_sentinels(tool_data)
    # The generators guarantee at least one masked identifier is present.
    assert embedded, "generators must embed at least one raw identifier"

    builder = ReasoningTraceBuilder()
    builder.record_step(data_summary=step_data, decision=decision)
    builder.record_tool_invocation(
        tool_name=tool_name, data_summary=tool_data, decision=decision
    )
    builder.record_decision(decision="final decision", data_summary=step_data)
    builder.record_tool_failure(tool_name=tool_name, unavailable_data=unavailable)
    # A realistic validator correction (size/confidence before/after — the
    # Correction dataclass never carries customer identifiers), recorded so the
    # trace's validator_corrections branch is exercised by full_trace().
    builder.record_validator_correction(
        Correction(
            invariant="single_half_size_cap",
            proposed_value="9.5",
            corrected_value="9.0",
        )
    )

    trace = builder.build(
        rationale=rationale,
        cited_signals=["oms_fit_returns"],
        confidence=confidence,
    )

    full = trace.full_trace()
    strings_in_trace = list(_iter_strings(full))

    # (1) No raw identifier value survives anywhere in the full trace. This walks
    # steps, validator corrections, failed tools, the customer-facing explanation
    # string, and masked_fields — recursively.
    for sentinel in embedded:
        for s in strings_in_trace:
            assert sentinel not in s, (
                f"raw identifier {sentinel!r} leaked into trace string {s!r}"
            )

    # (2) The customer-facing explanation (also surfaced in full_trace) never
    # carries a raw identifier.
    for sentinel in embedded:
        assert sentinel not in trace.customer_facing_explanation

    # (3) Wherever an identifier was redacted, the placeholder is present. Since
    # the generators always embed at least one masked key, the placeholder must
    # appear somewhere in the built trace.
    assert any(MASK_PLACEHOLDER in s for s in strings_in_trace), (
        "mask placeholder absent despite recorded identifiers"
    )

    # (4) masked_fields reports exactly the two direct identifiers.
    assert trace.masked_fields == MASKED_FIELDS
    assert set(trace.masked_fields) == {"customer_uid", "cnsmr_prof_sid"}
    assert full["masked_fields"] == list(MASKED_FIELDS)
