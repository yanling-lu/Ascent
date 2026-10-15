"""Structured-output parser for Claude's returned size decision.

Claude Opus returns a single structured object per size recommendation (design
"Structured Output Contract"):

```json
{
  "recommended_size": "7.5",
  "confidence_level": "medium",
  "size_rationale": "Plain-language explanation citing at least one historical signal.",
  "cited_signals": ["oms_fit_returns", "customer_fit_profile", "catalog_fit_guidance"],
  "width_warning": "This style runs narrow; consider the roomier toe box.",
  "comfort_note": "Prioritizes cushioning per the customer's comfort preference."
}
```

This module turns that raw model output — a JSON string, text with the JSON
embedded (prose or a fenced ```json block), or an already-parsed ``dict`` — into
the :class:`~ai_fit.reasoning.validator.ProposedDecision` the Guardrail Validator
consumes (design "Separation of concerns": Claude -> structured decision ->
validator). It is deliberately **defensive**: Claude is instructed to emit the
contract but a parser that assumes well-formed output would be the weakest link
between the model and the deterministic backstop, so malformed output is handled
explicitly rather than crashing the orchestrator.

``ProposedDecision`` is defined in :mod:`ai_fit.reasoning.validator`; it is
imported and reused here, never redefined.

Defensive behavior:

* ``recommended_size`` is coerced from JSON numbers *and* size strings like
  ``"7.5"`` / ``"7 1/2"`` to ``float``; a missing, empty, or non-numeric value is
  a hard error (the validator cannot clamp a size that is not a number).
* ``confidence_level`` is normalized case-insensitively to one of
  ``{low, medium, high}``; the validator also clamps it, so an unrecognized tier
  degrades to ``low`` rather than failing the whole parse.
* ``size_rationale`` falls back to an empty string; ``cited_signals`` is coerced
  to a tuple of non-empty strings; ``width_warning`` / ``comfort_note`` stay
  optional. These are advisory fields the validator recomputes or passes through,
  so lenient handling keeps a usable decision flowing to the backstop.
* Unparseable input (no JSON object found, not an object, or no usable size)
  raises :class:`ParseError`. For callers that prefer a value over an exception,
  :func:`try_parse_proposed_decision` returns a :class:`ParseResult` carrying
  either the decision or the error.

Requirements served: 7.2 (the parsed ``size_rationale`` / ``cited_signals`` carry
the historical-signal citation Claude produced through to the customer-facing
explanation) and 9.3 (recommendations flow from the model's structured decision,
derived from tool data, into the validator — not from prompt-stuffed data).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Sequence, Union

from ai_fit.reasoning.validator import ProposedDecision

__all__ = [
    "ParseError",
    "ParseResult",
    "parse_proposed_decision",
    "try_parse_proposed_decision",
]

# Canonical confidence tiers (mirrors ai_fit.reasoning.sizing LOW/MEDIUM/HIGH).
_LOW = "low"
_MEDIUM = "medium"
_HIGH = "high"
_VALID_TIERS = frozenset({_LOW, _MEDIUM, _HIGH})

# Pulls the first balanced-looking JSON object out of mixed text. Claude at
# temperature 0 under the contract usually returns bare JSON, but it may wrap it
# in prose or a fenced ```json block; this finds the outermost {...} span.
_OBJECT_SPAN = re.compile(r"\{.*\}", re.DOTALL)

# "7", "7.5", "7,5" (locale comma) with optional surrounding size words/units.
_DECIMAL = re.compile(r"[-+]?\d+(?:[.,]\d+)?")
# "7 1/2" or "7-1/2" style fractional sizes.
_MIXED_FRACTION = re.compile(r"(\d+)\s*[-\s]\s*(\d+)\s*/\s*(\d+)")
_SIMPLE_FRACTION = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")


class ParseError(ValueError):
    """Raised when Claude's output cannot be parsed into a ``ProposedDecision``.

    This means the input carried no usable JSON object or no usable
    ``recommended_size`` — a structural failure the lenient field handling cannot
    recover from. It subclasses :class:`ValueError` so existing ``except
    ValueError`` orchestrator guards also catch it.
    """


@dataclass(frozen=True)
class ParseResult:
    """Outcome of a non-raising parse (:func:`try_parse_proposed_decision`).

    Exactly one of ``decision`` / ``error`` is populated: ``ok`` is ``True`` with a
    :class:`ProposedDecision` on success, or ``False`` with the :class:`ParseError`
    (not raised) on failure, so callers can branch without a ``try``/``except``.
    """

    ok: bool
    decision: Optional[ProposedDecision] = None
    error: Optional[ParseError] = None


def parse_proposed_decision(
    raw: Union[str, bytes, Mapping[str, Any]],
) -> ProposedDecision:
    """Parse Claude's raw structured output into a :class:`ProposedDecision`.

    Accepts a JSON string, UTF-8 ``bytes``, text with the JSON object embedded
    (prose or a fenced code block), or an already-parsed mapping. Returns a
    ``ProposedDecision`` with every field defensively coerced.

    Raises :class:`ParseError` when the input contains no JSON object, when that
    object is not a JSON object (e.g. a bare array or number), or when
    ``recommended_size`` is missing/empty/non-numeric — none of which the
    downstream validator can meaningfully clamp.
    """
    obj = _to_object(raw)

    size = _coerce_size(obj.get("recommended_size"))
    confidence = _coerce_confidence(obj.get("confidence_level"))
    rationale = _coerce_text(obj.get("size_rationale"))
    cited = _coerce_cited_signals(obj.get("cited_signals"))
    width_warning = _coerce_optional_text(obj.get("width_warning"))
    comfort_note = _coerce_optional_text(obj.get("comfort_note"))

    return ProposedDecision(
        recommended_size=size,
        confidence_level=confidence,
        size_rationale=rationale,
        cited_signals=cited,
        width_warning=width_warning,
        comfort_note=comfort_note,
    )


def try_parse_proposed_decision(
    raw: Union[str, bytes, Mapping[str, Any]],
) -> ParseResult:
    """Non-raising variant of :func:`parse_proposed_decision`.

    Returns a :class:`ParseResult` with ``ok=True`` and the decision on success, or
    ``ok=False`` and the captured :class:`ParseError` on failure. Use this on the
    orchestrator path where a malformed model response should degrade gracefully
    rather than raise.
    """
    try:
        return ParseResult(ok=True, decision=parse_proposed_decision(raw))
    except ParseError as exc:
        return ParseResult(ok=False, error=exc)


# ---------------------------------------------------------------------------
# Object extraction
# ---------------------------------------------------------------------------


def _to_object(raw: Union[str, bytes, Mapping[str, Any]]) -> Mapping[str, Any]:
    """Return the JSON object from ``raw`` or raise :class:`ParseError`.

    Mappings are accepted as-is (already parsed upstream). Strings/bytes are first
    tried as whole JSON, then — if that is not an object — scanned for the first
    embedded ``{...}`` span, so prose or a fenced ```json block around the object
    still parses.
    """
    if isinstance(raw, Mapping):
        return raw

    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ParseError("model output was not valid UTF-8 text") from exc

    if not isinstance(raw, str):
        raise ParseError(
            f"expected str, bytes, or mapping, got {type(raw).__name__}"
        )

    text = raw.strip()
    if not text:
        raise ParseError("model output was empty")

    # Fast path: the whole string is a JSON object.
    parsed = _try_json(text)
    if isinstance(parsed, Mapping):
        return parsed
    if parsed is not None:
        # Valid JSON, but not an object (array, number, string, ...).
        raise ParseError(
            f"model output JSON was a {type(parsed).__name__}, expected an object"
        )

    # Slow path: find the first embedded {...} span and parse that.
    match = _OBJECT_SPAN.search(text)
    if match is not None:
        embedded = _try_json(match.group(0))
        if isinstance(embedded, Mapping):
            return embedded

    raise ParseError("no JSON object found in model output")


def _try_json(text: str) -> Any:
    """Parse ``text`` as JSON, returning ``None`` if it is not valid JSON."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Field coercion
# ---------------------------------------------------------------------------


def _coerce_size(value: Any) -> float:
    """Coerce ``recommended_size`` to ``float`` or raise :class:`ParseError`.

    Accepts JSON numbers and size strings — ``"7.5"``, ``"7,5"`` (locale comma),
    ``"7 1/2"``, ``"7"``, or a value with surrounding size words/units — because
    the contract types the size as a string and models vary in how they render a
    half size. ``None``, empty, boolean, or non-numeric input is a hard error:
    the validator snaps a real size to the offered grid, but it cannot invent one.
    """
    if value is None:
        raise ParseError("recommended_size is missing")
    # bool is an int subclass; a true/false size is nonsensical.
    if isinstance(value, bool):
        raise ParseError("recommended_size was a boolean, expected a size")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return _parse_size_string(value)
    raise ParseError(
        f"recommended_size had unsupported type {type(value).__name__}"
    )


def _parse_size_string(text: str) -> float:
    """Parse a size out of a string, tolerating fractions, commas, and units."""
    stripped = text.strip()
    if not stripped:
        raise ParseError("recommended_size was an empty string")

    # "1/2" style bare fraction (unlikely for a shoe size but handled).
    simple = _SIMPLE_FRACTION.match(stripped)
    if simple is not None:
        num, den = int(simple.group(1)), int(simple.group(2))
        if den == 0:
            raise ParseError(f"recommended_size {text!r} has a zero denominator")
        return num / den

    # "7 1/2" / "7-1/2" mixed fraction.
    mixed = _MIXED_FRACTION.search(stripped)
    if mixed is not None:
        whole, num, den = (int(mixed.group(i)) for i in (1, 2, 3))
        if den == 0:
            raise ParseError(f"recommended_size {text!r} has a zero denominator")
        return whole + num / den

    # Plain decimal, possibly embedded in words/units ("size 7.5", "7.5 US").
    decimal = _DECIMAL.search(stripped)
    if decimal is not None:
        token = decimal.group(0).replace(",", ".")
        try:
            return float(token)
        except ValueError as exc:  # pragma: no cover - regex guarantees numeric
            raise ParseError(
                f"recommended_size {text!r} is not a number"
            ) from exc

    raise ParseError(f"recommended_size {text!r} is not a number")


def _coerce_confidence(value: Any) -> str:
    """Normalize ``confidence_level`` to ``low|medium|high``.

    Lowercased and trimmed; an unrecognized or missing value degrades to ``low``
    rather than failing the parse, because the validator independently clamps and
    may cap the tier anyway (design Invariant 3). Returning the most conservative
    tier keeps a usable decision flowing while never overstating confidence.
    """
    if isinstance(value, str):
        tier = value.strip().lower()
        if tier in _VALID_TIERS:
            return tier
    return _LOW


def _coerce_text(value: Any) -> str:
    """Coerce an optional text field to a plain string, defaulting to ``""``."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _coerce_optional_text(value: Any) -> Optional[str]:
    """Coerce an optional note to a non-empty string or ``None``.

    Empty/whitespace-only strings collapse to ``None`` so an absent warning and a
    blank warning are indistinguishable downstream (both mean "no warning").
    """
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    text = text.strip()
    return text or None


def _coerce_cited_signals(value: Any) -> Sequence[str]:
    """Coerce ``cited_signals`` to a tuple of non-empty, de-duplicated strings.

    Accepts a list/tuple of signal names or a single string (wrapped into one
    element). Non-string entries are stringified; empties are dropped. Order is
    preserved so the trace surfaces signals as Claude cited them.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, (list, tuple)):
        out: List[str] = []
        seen = set()
        for item in value:
            text = (item if isinstance(item, str) else str(item)).strip()
            if text and text not in seen:
                seen.add(text)
                out.append(text)
        return tuple(out)
    # Any other scalar: stringify as a single cited signal.
    text = str(value).strip()
    return (text,) if text else ()
