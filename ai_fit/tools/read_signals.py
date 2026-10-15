"""Read Agent Tools (Task 7.2): review, recommendation-outcome, and quality tools.

This module implements three of the read tools in the design's Agent Tool Catalog:

* ``get_sku_reviews``             (tool #4) — review-derived fit/width/occasion-cue
                                   signals for a SKU (Requirements 3.1, 3.3-3.10).
* ``get_recommendation_outcomes`` (tool #5) — learned kept/returned outcome
                                   breakdown for a SKU (Requirements 4.3, 4.4, 4.5).
* ``get_quality_returns_and_units`` (tool #6) — per-SKU quality counts, units sold,
                                   and reason breakdown over the same OMS SKU key,
                                   across all SKUs (Requirements 5.1, 5.2, 5.10).

Each tool returns a plain, JSON-serializable structure (``dict`` / ``list`` of
``dict``) exactly matching the design's tool-catalog output shapes. Claude reasons
over these outputs and the validator/trace consume them, so nothing here returns a
typed dataclass — the records are projected into plain built-in types.

The review and quality tools read through the Task 2.2 data-layer mappers
(:mod:`ai_fit.data.mappers`), which are pure functions of an already-loaded
:class:`~ai_fit.data.store.DataStore`.

Recommendation outcomes come from a *separate* write store built in parallel
(Task 8.1, ``ai_fit/tools/outcomes.py``). To keep this module standalone and
decoupled from that file's existence, ``get_recommendation_outcomes`` reads through
a small :class:`OutcomeReader` protocol: anything that can report per-SKU
kept / returned-too-big / returned-too-small / returned-other counts satisfies it.
The real outcome store can be wired in later by passing it (or an adapter) as the
``outcome_reader`` argument; a trivial in-memory reader is provided for standalone
use and testing.
"""

from __future__ import annotations

from typing import Dict, List, Mapping, Protocol, runtime_checkable

from ai_fit.data.mappers import (
    map_all_quality_returns,
    map_sku_review_signals,
)
from ai_fit.data.store import DataStore

# ---------------------------------------------------------------------------
# Tool #4 — get_sku_reviews (Requirements 3.1, 3.3, 3.5, 3.9)
# ---------------------------------------------------------------------------

# Canonical review vocabularies, surfaced in a fixed order so the tool output is
# stable and every key is always present (zero-filled) — this gives Claude a
# predictable shape to reason over rather than a sparse dict that varies per SKU.
_FIT_KEYS = ("runs_small", "runs_large", "true_to_size")
_WIDTH_KEYS = ("normal", "narrow", "wide")


def get_sku_reviews(store: DataStore, sku: str) -> Dict[str, object]:
    """Return review-derived fit/width/occasion signals for a SKU (tool #4).

    Output shape (design Agent Tool Catalog #4)::

        {
          "review_count": int,
          "fit_signal_counts":   {"runs_small": int, "runs_large": int,
                                  "true_to_size": int},
          "width_signal_counts": {"normal": int, "narrow": int, "wide": int},
          "occasion_cues": [str, ...]        # canonical, de-duplicated
        }

    The fit/width count maps are fully keyed (missing categories reported as ``0``)
    so the shape is identical for every SKU. ``occasion_cues`` carries the
    normalized review occasions (Requirement 3.5/3.8); an absent SKU yields a
    ``review_count`` of ``0`` with empty cues and zeroed counts.

    Reads through :func:`ai_fit.data.mappers.map_sku_review_signals`, which already
    tallies the canonical ``fit_feedback`` / ``width_feedback`` values and
    normalizes occasion cues; this tool only projects that typed record into the
    plain JSON-serializable shape Claude consumes.
    """
    signals = map_sku_review_signals(store, sku)
    return {
        "review_count": signals.review_count,
        "fit_signal_counts": {
            key: int(signals.fit_signal_counts.get(key, 0)) for key in _FIT_KEYS
        },
        "width_signal_counts": {
            key: int(signals.width_signal_counts.get(key, 0)) for key in _WIDTH_KEYS
        },
        "occasion_cues": list(signals.occasion_cues),
    }


# ---------------------------------------------------------------------------
# Tool #5 — get_recommendation_outcomes (Requirements 4.3, 4.4, 4.5)
# ---------------------------------------------------------------------------


@runtime_checkable
class OutcomeReader(Protocol):
    """Minimal read interface over the recommendation-outcome store (Task 8.1).

    ``get_recommendation_outcomes`` depends only on this protocol, not on the
    concrete outcome store, so this tool is standalone and can be wired to the real
    store (``ai_fit/tools/outcomes.py``) later by passing an object that satisfies
    it. An implementation must return, for a given SKU, the counts of outcomes in
    each terminal bucket.

    The returned mapping must contain the integer keys ``kept``,
    ``returned_too_big``, ``returned_too_small``, and ``returned_other``. Any
    outcome still in the initial ``unknown`` state is excluded from these counts —
    only resolved outcomes feed back as Outcome_Fit_Signals (Requirement 4.3). An
    absent SKU returns all-zero counts (or an empty mapping, which this tool treats
    as all-zero).
    """

    def outcome_counts_for_sku(self, sku: str) -> Mapping[str, int]:
        """Return resolved outcome counts for ``sku`` (keys above; zeros if none)."""
        ...


# Fixed terminal-outcome buckets, in a stable order. ``unknown`` is deliberately
# excluded: it is the initial write state and not yet a learned signal.
_OUTCOME_KEYS = ("kept", "returned_too_big", "returned_too_small", "returned_other")


def get_recommendation_outcomes(
    outcome_reader: OutcomeReader, sku: str
) -> Dict[str, int]:
    """Return learned outcome signals for a SKU (tool #5).

    Output shape (design Agent Tool Catalog #5)::

        {
          "outcome_count": int,            # total resolved outcomes for the SKU
          "kept": int,
          "returned_too_big": int,
          "returned_too_small": int,
          "returned_other": int
        }

    These Outcome_Fit_Signals reinforce the next recommendation and count toward the
    supporting-signal total (Requirement 4.4); they never override OMS/review
    signals. Updated outcomes are reused in real time with no human approval
    (Requirement 4.5), which is why this is a plain read with no gating.

    ``outcome_reader`` is any object satisfying :class:`OutcomeReader` — the real
    outcome store from Task 8.1 (or an adapter over it), or the lightweight
    :class:`InMemoryOutcomeReader` for standalone use. ``outcome_count`` is the sum
    of the four terminal buckets, so a SKU with only ``unknown`` outcomes (or no
    outcomes) reports ``0``.
    """
    raw = outcome_reader.outcome_counts_for_sku(sku) or {}
    counts = {key: int(raw.get(key, 0)) for key in _OUTCOME_KEYS}
    counts_result: Dict[str, int] = {"outcome_count": sum(counts.values())}
    counts_result.update(counts)
    return counts_result


class InMemoryOutcomeReader:
    """A trivial in-memory :class:`OutcomeReader` for standalone use and testing.

    Holds a ``{sku -> {bucket -> count}}`` mapping and reports it directly. This lets
    ``get_recommendation_outcomes`` run and be exercised without the Task 8.1 write
    store; the real store (or an adapter over it) replaces this at wiring time.

    Unknown-state outcomes are the caller's responsibility to exclude when
    populating this reader — it reports whatever counts it is given.
    """

    def __init__(self, counts_by_sku: Mapping[str, Mapping[str, int]] | None = None):
        self._counts: Dict[str, Dict[str, int]] = {
            str(sku).strip(): {str(k): int(v) for k, v in buckets.items()}
            for sku, buckets in (counts_by_sku or {}).items()
        }

    def outcome_counts_for_sku(self, sku: str) -> Mapping[str, int]:
        """Return the stored buckets for ``sku`` (empty mapping if unknown)."""
        return self._counts.get(str(sku).strip(), {})


# ---------------------------------------------------------------------------
# Tool #6 — get_quality_returns_and_units (Requirements 5.1, 5.2, 5.10)
# ---------------------------------------------------------------------------


def get_quality_returns_and_units(store: DataStore) -> List[Dict[str, object]]:
    """Return per-SKU quality counts + units sold for all SKUs (tool #6).

    Takes no SKU input (report-wide); output shape (design Agent Tool Catalog #6)::

        [
          {
            "sku": str,
            "quality_count": int,
            "units_sold": int,
            "reason_breakdown": {               # only the four quality reasons
                "Defective": int, "Poor quality": int,
                "Arrived in poor condition": int, "Manufacturer defect": int
            }
          },
          ...
        ]

    Both ``quality_count`` and ``units_sold`` are computed on the **same OMS SKU
    key** (Requirement 5.10) by the Task 2.2 mapper
    :func:`ai_fit.data.mappers.map_all_quality_returns`, so the downstream report
    can form a valid ``quality_return_rate``. The ``reason_breakdown`` counts only
    the four whitelisted quality reasons (Requirement 5.2); fit/preference reasons
    are excluded at the mapper. SKUs are returned in sorted order (from the mapper)
    for a stable report input.
    """
    records = map_all_quality_returns(store)
    return [
        {
            "sku": record.sku,
            "quality_count": record.quality_count,
            "units_sold": record.units_sold,
            "reason_breakdown": dict(record.reason_breakdown),
        }
        for record in records
    ]
