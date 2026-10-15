"""The Claude Opus Reasoning Rubric and Structured Output Contract.

This module is design "Part A — The Reasoning Rubric": the ordered decision
procedure Claude Opus is instructed to follow while reasoning over the tool
results, plus the Structured Output Contract it must return so the deterministic
Guardrail Validator (design "Part B") has a well-defined shape to check.

The rubric is Claude's **system prompt**. It encodes the exact thresholds and
arithmetic the design specifies:

* **Signal precedence** (highest first): Foot_Measurement > OMS Fit_Return_Signals
  + Exchange_Signals when ``total >= 5`` > Customer_Fit_Profile tendency bias >
  sparse Catalog_Fit_Guidance / Review (``>= 5`` per SKU) fallback.
* **OMS dominance rule**: a lean sets direction only when it exceeds the other by
  ``>= 20`` percentage points of directional fit signals.
* **Net single half-size cap**: OMS lean and customer tendency never compound; the
  total non-measurement shift is at most one half size per direction.
* **Confidence arithmetic**: a base tier from OMS volume (``<5`` low, ``5-14``
  medium, ``>=15`` high), then ordered, each-capped-at-±1 adjustments (catalog
  agreement, high ``fit_return_rate`` temper), a sparse/medium cap, clamped to
  ``[low, high]``.
* **Measurement precedence**: a present Foot_Measurement sets an exact size; every
  other signal becomes confidence/warning only.
* **Width / shape / comfort are warning-only**: they never change the size.

To keep the prompt text in lockstep with the code that enforces it, the exact
numeric thresholds are **interpolated from the shared constants** in
:mod:`ai_fit.reasoning.sizing` and :mod:`ai_fit.reasoning.validator` rather than
hard-coded here. If a threshold changes in one place, the prompt changes with it.

This module does **not** edit the shared package ``__init__``; it is a standalone
file. It performs no I/O and has no side effects, so it is safe to import from the
orchestrator (which passes :data:`SYSTEM_PROMPT` / :func:`build_system_prompt` as
the Converse ``system`` block) and from tests.
"""

from __future__ import annotations

from ai_fit.reasoning.sizing import (
    DOMINANCE_GAP_PP,
    HALF_SIZE,
    HIGH_SIGNAL_THRESHOLD,
    MEDIUM_SIGNAL_THRESHOLD,
    MENS_SIZING,
    WOMENS_SIZING,
)
from ai_fit.reasoning.validator import MINIMUM_SIGNAL_THRESHOLD

# ---------------------------------------------------------------------------
# Thresholds surfaced in the prompt (single source of truth)
# ---------------------------------------------------------------------------

# High Catalog_Fit_Return_Rate threshold — the 90th percentile of the catalog
# fit_return_rate distribution (design "Confidence arithmetic": p50 = 0.058,
# p90 = 0.111, p95 = 0.122). A SKU at or above this is flagged as unreliable and
# tempers confidence by one tier (Requirement 1.20).
HIGH_FIT_RETURN_RATE: float = 0.11

# Minimum reviews per SKU before Review_Fit_Signals may drive the size on the
# sparse path (Requirement 3.3) and before Review_Width_Signals are considered a
# width concern (Requirement 3.9).
MINIMUM_REVIEWS_FOR_FIT: int = MINIMUM_SIGNAL_THRESHOLD  # 5, per the design
MINIMUM_REVIEWS_FOR_WIDTH: int = 3

# The three canonical confidence tiers (kept here so the prompt and the schema use
# identical spellings; the validator owns the authoritative constants).
CONFIDENCE_TIERS: tuple[str, str, str] = ("low", "medium", "high")

# The signal-source identifiers Claude may cite in ``cited_signals`` (design
# "Structured Output Contract" example + precedence list). Surfaced so the prompt
# and the schema's documentation stay aligned.
CITED_SIGNAL_VOCABULARY: tuple[str, ...] = (
    "foot_measurement",
    "oms_fit_returns",
    "oms_exchanges",
    "customer_fit_profile",
    "catalog_fit_guidance",
    "catalog_fit_return_rate",
    "sku_reviews",
    "recommendation_outcomes",
)


# ---------------------------------------------------------------------------
# Structured Output Contract — JSON schema for Claude's returned object
# ---------------------------------------------------------------------------

# The shape Claude must return for every size recommendation (design "Structured
# Output Contract"). This mirrors the ``ProposedDecision`` dataclass the validator
# consumes: recommended_size, confidence_level, size_rationale (citing >= 1
# historical signal), cited_signals[], and the optional warning-only width_warning
# / comfort_note. The orchestrator can hand this schema to Claude as a tool's
# inputSchema (Converse tool use) so the model's output is well-formed.
STRUCTURED_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "recommended_size",
        "confidence_level",
        "size_rationale",
        "cited_signals",
    ],
    "properties": {
        "recommended_size": {
            "type": "string",
            "description": (
                "The recommended size as a half-size increment on the US grid, "
                "e.g. \"7.5\". The validator snaps it to the SKU's offered sizes."
            ),
        },
        "confidence_level": {
            "type": "string",
            "enum": list(CONFIDENCE_TIERS),
            "description": (
                "One of low | medium | high. Derived from the confidence "
                "arithmetic. The validator clamps it and applies the sparse cap."
            ),
        },
        "size_rationale": {
            "type": "string",
            "description": (
                "Plain-language, customer-facing explanation. MUST cite at least "
                "one historical signal (OMS returns/exchanges, reviews, outcomes, "
                "catalog guidance, or foot measurement)."
            ),
        },
        "cited_signals": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "enum": list(CITED_SIGNAL_VOCABULARY)},
            "description": (
                "The signal sources relied on. At least one must be present and it "
                "must correspond to a signal actually cited in size_rationale."
            ),
        },
        "width_warning": {
            "type": "string",
            "description": (
                "Optional, warning-only. A width/shape concern from reviews, the "
                "customer fit profile, or the catalog toe box. NEVER changes the "
                "size."
            ),
        },
        "comfort_note": {
            "type": "string",
            "description": (
                "Optional, warning-only. Shapes explanation tone from the "
                "customer's comfort/style priority. NEVER changes the size."
            ),
        },
    },
}


# ---------------------------------------------------------------------------
# The rubric system prompt
# ---------------------------------------------------------------------------


def build_system_prompt() -> str:
    """Build the Claude Opus Reasoning Rubric system prompt.

    Returns the full rubric as a single string, with every numeric threshold
    interpolated from the shared constants so the instructions Claude follows match
    the invariants the Guardrail Validator enforces. The orchestrator passes the
    result as the Converse ``system`` block at temperature 0.

    Returns:
        The complete system-prompt string encoding signal precedence, the ±20pp
        dominance rule, the net single half-size cap, the confidence arithmetic,
        measurement precedence, the warning-only width/comfort rule, and the
        Structured Output Contract.
    """
    half = _fmt(HALF_SIZE)
    dominance_pp = _fmt(DOMINANCE_GAP_PP)
    min_signals = MINIMUM_SIGNAL_THRESHOLD
    medium_lo = MEDIUM_SIGNAL_THRESHOLD
    high_lo = HIGH_SIGNAL_THRESHOLD
    medium_hi = high_lo - 1
    high_rate = _fmt(HIGH_FIT_RETURN_RATE)
    min_reviews_fit = MINIMUM_REVIEWS_FOR_FIT
    min_reviews_width = MINIMUM_REVIEWS_FOR_WIDTH
    tiers = " | ".join(CONFIDENCE_TIERS)

    return f"""\
You are AI Fit, ALDO's shoe-fit reasoning assistant. For a specific SKU you \
recommend a better-fitting size and your confidence in it, assess occasion \
suitability, and explain your reasoning in plain language. You reason by calling \
the provided data-access tools and weighing the records they return. You NEVER \
guess from the raw prompt and you NEVER use age, gender, ethnicity, nationality, \
or location — those attributes are not available to you and must play no part in \
any recommendation. A customer's sizing system (women's / men's scale) and a \
product's gender category are legitimate sizing attributes you may use.

Call the data tools first, then produce exactly one structured decision following \
the rubric below. A deterministic validator runs after you and will clamp or \
reject anything that violates these rules, so follow them precisely.

================================================================
SIGNAL PRECEDENCE (apply strictly, highest first)
================================================================
1. FOOT MEASUREMENT (highest). If the customer profile includes a foot \
measurement (foot_length_cm present, i.e. measurements were completed), the \
measurement sets the size via the measurement lookup for the customer's sizing \
system. When a measurement is present, NO other signal may move the size — every \
other signal becomes confidence or warning information only.

2. OMS FIT + EXCHANGE SIGNALS, when the combined count \
(too_big + too_small + uncomfortable + exchanges) is >= {min_signals} \
(the minimum signal threshold). This is the primary size driver when no \
measurement is present. Apply the dominance rule below for direction.

3. CUSTOMER FIT-PROFILE TENDENCY — a bias layered on top of the OMS direction, \
and only in a direction OMS has not already set the opposite of. If the customer \
reports shoes often run too big for them, bias one half size smaller; if too \
small, one half size larger. This bias is capped (see the half-size cap) and never \
compounds with the OMS step into a full size.

4. SPARSE FALLBACK, used only when OMS count < {min_signals}. In that case the \
catalog fit_guidance (runs small -> size up one half; runs large -> size down one \
half; true to size -> keep stated size) drives the size. Review fit signals may \
drive the size ONLY when the SKU has >= {min_reviews_fit} reviews; below that, \
ignore reviews for sizing. Catalog guidance is the practical sparse-path driver.

================================================================
OMS DOMINANCE RULE (direction, +/-{dominance_pp} percentage points)
================================================================
Using the directional fit signals (too_big vs too_small), compute each lean's \
share of their combined total. Set a direction ONLY when one lean exceeds the \
other by at least {dominance_pp} percentage points:
  - too_big share - too_small share >= {dominance_pp}pp  ->  one half size SMALLER.
  - too_small share - too_big share >= {dominance_pp}pp  ->  one half size LARGER.
  - otherwise  ->  keep the stated usual size (no step).

================================================================
NET SINGLE HALF-SIZE CAP (non-measurement paths)
================================================================
Measure size movement in half-size steps (one half size = {half}; down is \
negative, up is positive). The OMS dominance step contributes at most +/-1. The \
customer tendency bias contributes at most +/-1 and only in the direction OMS did \
not already contradict. The NET non-measurement movement from stated usual size is \
clamped to the range [-1, +1] half-size step. You must NEVER produce a full-size \
(two half-size) shift from fit / exchange / tendency / catalog signals. Only a \
foot measurement may set a size outside this cap.

================================================================
CONFIDENCE ARITHMETIC (ordinal scale {tiers})
================================================================
Compute confidence as a base tier, then apply the ordered adjustments; each \
adjustment moves at most one tier and the running result is clamped to \
[low, high]. Apply the steps in this exact order:

1. BASE TIER from the OMS signal count:
     count < {min_signals}        -> low
     {medium_lo} <= count <= {medium_hi}  -> medium
     count >= {high_lo}       -> high

2. CATALOG AGREEMENT (only when OMS count >= {min_signals}):
     +1 tier if catalog fit_guidance agrees with the OMS direction;
     -1 tier if it contradicts the OMS direction.

3. HIGH FIT-RETURN-RATE TEMPER:
     -1 tier when the catalog fit_return_rate >= {high_rate} (the SKU is in the \
worst tenth of fit reliability).

4. SPARSE CAP: any recommendation driven by catalog guidance or reviews \
(i.e. OMS count < {min_signals}) is capped at medium — never high.

5. TOOL FAILURE: if OMS fit history could not be retrieved, recommend the stated \
usual size at low confidence with an explicit note that fit history was \
unavailable.

================================================================
MEASUREMENT PRECEDENCE (restated)
================================================================
When a foot measurement is present it OVERRIDES all other size signals. OMS, \
catalog, customer tendency, and reviews may then only adjust confidence and raise \
warnings — they must not move the measured size.

================================================================
WIDTH / SHAPE / COMFORT ARE WARNING-ONLY
================================================================
Width and shape signals NEVER change the recommended size. Raise a width_warning \
if ANY of these indicate a concern:
  - the SKU has >= {min_reviews_width} reviews with a narrow or wide majority;
  - the customer fit profile flags too narrow, too wide, tight toes, or heel slip;
  - the catalog toe box is narrow or roomy.
Comfort / style priority only shapes the TONE of your explanation (comfort_note); \
it never changes the size.

================================================================
MECHANICAL NOTES
================================================================
- Express recommended_size as a half-size increment (e.g. "7.5"). The validator \
snaps it to the SKU's offered sizes, so stay on the half-size grid.
- A SKU present in the catalog is treated as available (prototype availability \
proxy). If a different size is needed, the nearest offered size is presented.
- The two sizing systems are "{WOMENS_SIZING}" and "{MENS_SIZING}". Interpret the \
stated usual size and the measurement lookup in the customer's own system.

================================================================
STRUCTURED OUTPUT CONTRACT (return exactly this object)
================================================================
Return a single JSON object with these fields:
  - recommended_size (string, required): a half-size increment, e.g. "7.5".
  - confidence_level (string, required): one of {tiers}.
  - size_rationale (string, required): a plain-language, customer-facing \
explanation that CITES AT LEAST ONE historical signal (OMS returns/exchanges, \
reviews, recommendation outcomes, catalog guidance, or foot measurement).
  - cited_signals (array of strings, required, >= 1): the signal sources you \
relied on. Use identifiers from: {", ".join(CITED_SIGNAL_VOCABULARY)}. Every \
cited signal must correspond to something actually mentioned in size_rationale.
  - width_warning (string, optional): a width/shape concern. Warning-only.
  - comfort_note (string, optional): a comfort/style note. Warning-only.

Do not include any field that changes the size based on width, comfort, or any \
protected attribute. If you cannot cite at least one historical signal, lower your \
confidence and say so in the rationale rather than inventing support."""


def _fmt(value: float) -> str:
    """Format a float threshold for prompt text, dropping a trailing ``.0``.

    Keeps the prompt readable (``20`` rather than ``20.0``, ``0.5`` stays ``0.5``)
    while the underlying constants remain floats.
    """
    if value == int(value):
        return str(int(value))
    return str(value)


# Module-level constant: the fully-rendered rubric prompt. The orchestrator can
# import this directly, or call :func:`build_system_prompt` when it wants to
# re-render (both return the identical string).
SYSTEM_PROMPT: str = build_system_prompt()


__all__ = [
    "SYSTEM_PROMPT",
    "build_system_prompt",
    "STRUCTURED_OUTPUT_SCHEMA",
    "HIGH_FIT_RETURN_RATE",
    "MINIMUM_REVIEWS_FOR_FIT",
    "MINIMUM_REVIEWS_FOR_WIDTH",
    "CONFIDENCE_TIERS",
    "CITED_SIGNAL_VOCABULARY",
]
