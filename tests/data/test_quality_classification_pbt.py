"""Property-based test for quality return-reason classification (Task 8.4).

Feature: ai-fit, Property 11: Quality classification matches the reason whitelist.

For any return reason, ``classify_quality_reason`` classifies it as a
Quality_Return_Signal (returning its canonical label) *if and only if* the reason
is one of the four whitelisted quality reasons:

* "Defective"
* "Poor quality"
* "Arrived in poor condition"
* "Manufacturer defect"

Every other reason — fit/preference reasons such as "Too big", "Too small",
"Uncomfortable", "Changed mind", and arbitrary junk — classifies as ``None``.

Classification tolerates arbitrary casing and the ``return.page.reasons.`` prefix
variant seen in the raw OMS reason distribution, so e.g. "DEFECTIVE",
"return.page.reasons.Defective", and "  Defective " all collapse to the canonical
"Defective".

The reference whitelist below is written out independently from the module
internals so the property is a genuine cross-check rather than a tautology.

**Validates: Requirements 5.2**
"""

from __future__ import annotations

from typing import Optional

from hypothesis import given, settings
from hypothesis import strategies as st

from ai_fit.data.mappers import classify_quality_reason

# ---------------------------------------------------------------------------
# Reference vocabularies (independent of mappers internals)
# ---------------------------------------------------------------------------

# The four whitelisted quality reasons mapped to their canonical labels. The key
# is the canonical phrase (as a user would type it); matching is case-insensitive
# and prefix-tolerant, so these keys are also the canonical label values.
_QUALITY_WHITELIST: dict[str, str] = {
    "Defective": "Defective",
    "Poor quality": "Poor quality",
    "Arrived in poor condition": "Arrived in poor condition",
    "Manufacturer defect": "Manufacturer defect",
}

# Fit/preference reasons and other junk that must NEVER classify as a quality
# return. These are chosen to not collide (even after lowercasing / stripping the
# prefix) with any whitelisted quality phrase.
_NON_QUALITY_REASONS: list[str] = [
    # Fit reasons.
    "Too big",
    "Too small",
    "Uncomfortable",
    # Preference reasons.
    "Changed mind",
    "Changed my mind",
    "Found a better price",
    "Wrong item",
    "Did not like style",
    "Ordered multiple sizes",
    "Late delivery",
    # Near-miss / partial phrases that must not match the whitelist.
    "quality",
    "defect",
    "poor",
    "condition",
    "manufacturer",
    # Degenerate inputs.
    "",
    "   ",
    "n/a",
    "unknown reason",
    "123",
]

_PREFIX = "return.page.reasons."


def _recased(phrase: str) -> st.SearchStrategy[str]:
    """Strategy producing casing variants of ``phrase`` (identity/lower/upper/title)."""
    return st.sampled_from([phrase, phrase.lower(), phrase.upper(), phrase.title()])


def _decorated(phrase: str) -> st.SearchStrategy[str]:
    """Re-case ``phrase``, optionally add the OMS prefix, and pad with whitespace.

    Mirrors the real-world raw OMS reason variants the classifier must tolerate:
    casing differences, the ``return.page.reasons.`` prefix, and stray whitespace.
    """
    ws = st.sampled_from(["", " ", "  ", "\t", " \t ", "\n"])

    def build(cased: str, prefixed: bool, lead: str, trail: str) -> str:
        body = f"{_PREFIX}{cased}" if prefixed else cased
        return f"{lead}{body}{trail}"

    return st.builds(build, _recased(phrase), st.booleans(), ws, ws)


def _quality_phrase_and_label() -> st.SearchStrategy[tuple[str, str]]:
    """Strategy yielding (messy raw quality reason, expected canonical label)."""
    return st.sampled_from(list(_QUALITY_WHITELIST.items())).flatmap(
        lambda item: _decorated(item[0]).map(lambda raw: (raw, item[1]))
    )


def _non_quality_reason() -> st.SearchStrategy[str]:
    """Strategy yielding a messy non-quality reason (should classify as None)."""
    return st.sampled_from(_NON_QUALITY_REASONS).flatmap(_decorated)


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


@settings(max_examples=300)
@given(_quality_phrase_and_label())
def test_whitelisted_reasons_classify_to_canonical_label(
    example: tuple[str, str],
) -> None:
    """Feature: ai-fit, Property 11 — each whitelisted reason yields its label.

    Casing and the ``return.page.reasons.`` prefix are tolerated, so every variant
    of a whitelisted reason maps to its canonical quality label.
    """
    raw, expected_label = example
    assert classify_quality_reason(raw) == expected_label


@settings(max_examples=300)
@given(_non_quality_reason())
def test_non_quality_reasons_classify_to_none(raw: str) -> None:
    """Feature: ai-fit, Property 11 — every non-whitelisted reason yields None.

    Fit reasons, preference reasons, partial phrases, and junk never classify as a
    Quality_Return_Signal regardless of casing or prefix.
    """
    assert classify_quality_reason(raw) is None


@settings(max_examples=300)
@given(st.one_of(_quality_phrase_and_label().map(lambda t: t[0]), _non_quality_reason()))
def test_classification_is_iff_whitelist_membership(raw: str) -> None:
    """Feature: ai-fit, Property 11 — classified as quality IFF on the whitelist.

    The classifier returns a non-None label exactly when the normalized reason is
    one of the four whitelisted phrases; the returned label (when any) is always a
    canonical whitelist value.
    """
    result: Optional[str] = classify_quality_reason(raw)

    # Independent normalization: lowercase, strip the prefix, strip whitespace.
    normalized = raw.strip().lower()
    if _PREFIX.lower() in normalized:
        normalized = normalized.split(_PREFIX.lower(), 1)[1]
    normalized = normalized.strip()

    expected_canonical = {k.lower(): v for k, v in _QUALITY_WHITELIST.items()}
    on_whitelist = normalized in expected_canonical

    assert (result is not None) == on_whitelist
    if result is not None:
        assert result == expected_canonical[normalized]
        assert result in _QUALITY_WHITELIST.values()
