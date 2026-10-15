"""Quality Issue Report tools (Task 8.3).

This module implements two of the design's Agent Tools (tool catalog entries #6 and
#8) plus the pure report logic that sits between them:

* :func:`get_quality_returns_and_units` — tool #6. Returns per-SKU quality counts and
  units sold for **all** SKUs (the report input), keyed on the same OMS SKU key so the
  rate is a valid ratio (Requirements 5.1, 5.2, 5.10).
* :func:`generate_quality_issue_report` — tool #8. Computes and returns the ranked
  Quality_Issue_Report ``{flagged[], none_flagged}`` (Requirements 5.3-5.9).

Everything is computed entirely from OMS-keyed data (Requirement 5.10) by reusing the
data layer: :func:`ai_fit.data.mappers.map_all_quality_returns` produces one
:class:`~ai_fit.data.mappers.QualityReturns` record per SKU — classification of the
four whitelisted quality reasons and the ``quality_return_rate`` property already live
there, so this module does not re-derive them.

The report is **informational only**: it computes and returns a payload and takes no
product-modifying action — no delist, reprice, or removal (Requirement 5.7). It carries
**no customer identifiers** — the inputs are already per-SKU aggregates with no customer
field, and nothing here reintroduces one (Requirement 5.9).

Design reference: "Quality Issue Report" section; tool catalog entries #6 and #8.
"""

from __future__ import annotations

from typing import Dict, List, Sequence

from ai_fit.data.mappers import QualityReturns, map_all_quality_returns
from ai_fit.data.store import DataStore

# ---------------------------------------------------------------------------
# Flag criterion constants (Requirement 5.3; design "Quality Issue Report").
# ---------------------------------------------------------------------------

#: A SKU is flagged outright once its quality-return count reaches this value.
QUALITY_COUNT_FLAG_THRESHOLD = 5

#: Minimum Units_Sold before the rate criterion may apply (Quality_Return_Rate
#: glossary entry: the 25th-percentile units-sold floor).
UNITS_SOLD_FLOOR = 30

#: Minimum Quality_Return_Rate (as a fraction, i.e. 5%) for the rate criterion.
QUALITY_RATE_THRESHOLD = 0.05

#: Explicit empty-case message (Requirement 5.8; design empty case).
NONE_FLAGGED_MESSAGE = "No SKUs currently meet the quality-issue thresholds."


# ---------------------------------------------------------------------------
# Tool #6: get_quality_returns_and_units
# ---------------------------------------------------------------------------


def get_quality_returns_and_units(store: DataStore) -> List[Dict[str, object]]:
    """Return quality returns + units sold for all SKUs (tool catalog entry #6).

    Output shape per the design tool contract:
    ``[{sku, quality_count, units_sold, reason_breakdown{...}}]`` — the raw report
    input. Only the four whitelisted quality reasons are counted (Requirement 5.2),
    and ``units_sold`` comes from the same OMS SKU key (Requirement 5.10); both are
    already enforced by the data layer. No customer identifiers appear in the output
    (Requirement 5.9).
    """
    return [_returns_payload(record) for record in map_all_quality_returns(store)]


def _returns_payload(record: QualityReturns) -> Dict[str, object]:
    """Project a :class:`QualityReturns` into the tool #6 output dict."""
    return {
        "sku": record.sku,
        "quality_count": record.quality_count,
        "units_sold": record.units_sold,
        "reason_breakdown": dict(record.reason_breakdown),
    }


# ---------------------------------------------------------------------------
# Flagging + ranking logic (pure; operates on QualityReturns records)
# ---------------------------------------------------------------------------


def _is_count_flagged(record: QualityReturns) -> bool:
    """``True`` when the SKU meets the count criterion (Requirement 5.3)."""
    return record.quality_count >= QUALITY_COUNT_FLAG_THRESHOLD


def _is_rate_flagged(record: QualityReturns) -> bool:
    """``True`` when the SKU meets the units-floor + rate criterion (Requirement 5.3)."""
    rate = record.quality_return_rate
    return (
        record.units_sold >= UNITS_SOLD_FLOOR
        and rate is not None
        and rate >= QUALITY_RATE_THRESHOLD
    )


def _is_flagged(record: QualityReturns) -> bool:
    """A SKU is flagged when it meets the count OR the rate criterion (Requirement 5.3)."""
    return _is_count_flagged(record) or _is_rate_flagged(record)


def _flagged_payload(record: QualityReturns) -> Dict[str, object]:
    """Build the per-SKU flagged payload (Requirement 5.4).

    Includes the SKU, its quality-return count, units sold, the Quality_Return_Rate,
    and the per-reason breakdown. Carries no customer identifiers (Requirement 5.9).
    ``flagged_by`` names which criterion fired (``"count"`` vs. ``"rate"``) so the
    report is self-describing; a SKU meeting both is reported as count-flagged since
    the count criterion dominates the ranking.
    """
    return {
        "sku": record.sku,
        "quality_count": record.quality_count,
        "units_sold": record.units_sold,
        "quality_return_rate": record.quality_return_rate,
        "reason_breakdown": dict(record.reason_breakdown),
        "flagged_by": "count" if _is_count_flagged(record) else "rate",
    }


def _rank_flagged(records: Sequence[QualityReturns]) -> List[QualityReturns]:
    """Rank flagged records per Requirement 5.5.

    Count-flagged SKUs come first, ordered by ``quality_count`` descending; SKUs
    flagged *solely* by rate come after all count-flagged ones. Ties break on the SKU
    key for a stable, deterministic order.
    """

    def sort_key(record: QualityReturns) -> tuple[int, int, str]:
        # group 0 = count-flagged (ranked first); group 1 = rate-only.
        group = 0 if _is_count_flagged(record) else 1
        # negate the count so a higher count sorts earlier within its group.
        return (group, -record.quality_count, record.sku)

    return sorted(records, key=sort_key)


# ---------------------------------------------------------------------------
# Tool #8: generate_quality_issue_report
# ---------------------------------------------------------------------------


def generate_quality_issue_report(store: DataStore) -> Dict[str, object]:
    """Compute and return the ranked Quality_Issue_Report (tool catalog entry #8).

    Output shape per the design tool contract: ``{flagged[], none_flagged:boolean}``.
    A SKU is flagged when ``quality_count >= 5`` OR (``units_sold >= 30`` AND
    ``quality_return_rate >= 5%``) (Requirement 5.3). The ``flagged`` list is ranked
    descending by ``quality_count`` with rate-only SKUs placed after count-flagged
    ones (Requirement 5.5), and each entry carries the SKU, count, units sold, rate,
    and per-reason breakdown (Requirement 5.4).

    This is **informational only**: it returns a payload and performs no
    product-modifying action (Requirement 5.7), and it excludes customer identifiers
    (Requirement 5.9). When nothing is flagged, ``none_flagged`` is ``True`` and an
    explicit ``message`` states that no SKUs meet the thresholds (Requirement 5.8).
    """
    all_records = map_all_quality_returns(store)
    flagged_records = [record for record in all_records if _is_flagged(record)]
    ranked = _rank_flagged(flagged_records)

    report: Dict[str, object] = {
        "flagged": [_flagged_payload(record) for record in ranked],
        "none_flagged": not ranked,
    }
    if not ranked:
        report["message"] = NONE_FLAGGED_MESSAGE
    return report
