"""Data-model mappers: raw CSV rows -> the design's typed records (Task 2.2).

This module sits on top of :mod:`ai_fit.data.store`. The store loads the six CSVs
into protected-attribute-free raw ``dict`` rows (keyed by normalized column names)
and builds the SKU/customer/order indexes; this module maps those raw rows into
the design's typed records:

* :class:`CustomerProfile`      — from the customer-profile dataset.
* :class:`ProductCatalogEntry`  — from the product-catalog dataset.
* :class:`SkuFitSignals`        — OMS fit returns joined with in-store exchanges
                                  on ``Original Order Number`` -> ``order_number``.
* :class:`SkuReviewSignals`     — from the reviews dataset.
* :class:`QualityReturns`       — quality counts + ``units_sold`` computed from the
                                  **same OMS SKU key** (Requirement 5.10).

The mappers are pure functions of the store's already-loaded rows: they perform no
I/O and no mutation of the underlying rows, so they are safe to call repeatedly.
Protected attributes are already stripped by the store, so nothing here needs to
re-filter them — but these records deliberately only project the legitimate fields
the design's tool catalog exposes.

Design references:
* Data Models section (exact field mappings).
* Requirements 1.1, 1.11, 1.16, 3.1, 5.1, 5.10.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence

from ai_fit.data.store import DataStore
from ai_fit.reasoning.occasion import (
    normalize_catalog_occasions,
    normalize_profile_occasions,
    normalize_review_occasion,
)

# ---------------------------------------------------------------------------
# Small parsing helpers
# ---------------------------------------------------------------------------


def _clean(value: object) -> str:
    """Return a stripped string for a raw CSV cell (``None`` -> empty string)."""
    if value is None:
        return ""
    return str(value).strip()


def _flag(value: object) -> bool:
    """Interpret a raw ``0``/``1`` (or ``true``/``yes``) cell as a boolean.

    The customer fit-profile and ``measurements_completed`` columns are stored as
    the strings ``"0"`` / ``"1"``. Anything that is not an affirmative token is
    treated as ``False`` so a blank cell never reads as a set flag.
    """
    return _clean(value).lower() in {"1", "true", "yes", "y"}


def _float_or_none(value: object) -> Optional[float]:
    """Parse a cell to ``float``, or ``None`` when empty / non-numeric."""
    text = _clean(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _int_or_zero(value: object) -> int:
    """Parse a cell to ``int`` (via ``float`` to tolerate ``"2.0"``), else ``0``."""
    text = _clean(value)
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# fit_guidance normalization (Requirement 1.16, design Data Models note)
# ---------------------------------------------------------------------------

# Canonical Catalog_Fit_Guidance directions (Requirement 1.16; Glossary entry
# "Catalog_Fit_Guidance" normalizes to exactly these three values).
RUNS_SMALL = "runs small"
RUNS_LARGE = "runs large"
TRUE_TO_SIZE = "true to size"


def normalize_fit_guidance(raw: object) -> Optional[str]:
    """Normalize free-text catalog ``fit_guidance`` to a canonical direction.

    The dataset stores guidance as a sentence, e.g. "Runs small - consider sizing
    up from your usual size". We classify by the leading direction phrase into one
    of ``runs small`` / ``runs large`` / ``true to size``. Returns ``None`` when the
    text is empty or matches no known direction, so callers can treat a SKU as
    having no catalog guidance rather than guessing.

    Matching is intentionally phrase-based (not a bare "small"/"large" substring)
    to avoid misreading incidental words; "true to size" is checked before the
    size-direction phrases.
    """
    text = _clean(raw).lower()
    if not text:
        return None
    if "true to size" in text:
        return TRUE_TO_SIZE
    if "runs small" in text or "size up" in text or "sizing up" in text:
        return RUNS_SMALL
    if "runs large" in text or "size down" in text or "sizing down" in text:
        return RUNS_LARGE
    return None


# ---------------------------------------------------------------------------
# toe_box normalization (Catalog_Toe_Box: regular | narrow | roomy)
# ---------------------------------------------------------------------------


def _normalize_toe_box(raw: object) -> Optional[str]:
    """Normalize ``toe_box`` to ``regular`` / ``narrow`` / ``roomy`` (lowercased)."""
    text = _clean(raw).lower()
    if text in {"regular", "narrow", "roomy"}:
        return text
    return None


# ---------------------------------------------------------------------------
# OMS return-reason classification
# ---------------------------------------------------------------------------

# Fit_Return_Signal reasons map to a fit direction. The data carries casing and
# "return.page.reasons."-prefixed quoting variants (see the OMS reason
# distribution), so we match on the trailing phrase case-insensitively.
_FIT_REASONS = {
    "too big": "too_big",
    "too small": "too_small",
    "uncomfortable": "uncomfortable",
}

# Quality_Return_Signal reasons (Requirement 5.2 / Glossary Quality_Return_Signal):
# exactly these four, with casing/quoting variants tolerated.
_QUALITY_REASONS = {
    "defective": "Defective",
    "poor quality": "Poor quality",
    "arrived in poor condition": "Arrived in poor condition",
    "manufacturer defect": "Manufacturer defect",
}


def _reason_key(raw: object) -> str:
    """Normalize a raw OMS reason to a lowercase trailing phrase for matching.

    Handles the ``return.page.reasons.Too big`` prefix variant and surrounding
    whitespace/casing so "Too big", "return.page.reasons.Too big", and
    "TOO BIG" all collapse to ``"too big"``.
    """
    text = _clean(raw).lower()
    marker = "return.page.reasons."
    if marker in text:
        text = text.split(marker, 1)[1]
    return text.strip()


def classify_fit_reason(raw: object) -> Optional[str]:
    """Return ``too_big`` / ``too_small`` / ``uncomfortable`` for a fit reason, else ``None``."""
    return _FIT_REASONS.get(_reason_key(raw))


def classify_quality_reason(raw: object) -> Optional[str]:
    """Return the canonical quality-reason label for a quality reason, else ``None``.

    Only the four whitelisted quality reasons classify; every fit/preference reason
    yields ``None`` (Requirement 5.2).
    """
    return _QUALITY_REASONS.get(_reason_key(raw))


# ---------------------------------------------------------------------------
# Typed records (design "Data Models")
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CustomerFitProfile:
    """Boolean fit-tendency flags from the Customer_Profile (design fit_profile)."""

    too_big: bool = False
    too_small: bool = False
    too_narrow: bool = False
    too_wide: bool = False
    tight_toes: bool = False
    heels_slip: bool = False
    no_issues: bool = False
    not_sure: bool = False


@dataclass(frozen=True)
class CustomerProfile:
    """Customer fit profile (Requirement 1.11; design CustomerProfile).

    Protected attributes are never present (stripped at the store). ``sizing_system``
    is the retained legitimate attribute (Requirement 10.5). ``foot_length_cm`` /
    ``foot_width_cm`` are populated **only** when ``measurements_completed = 1``
    (design Data Models), otherwise ``None``.
    """

    synthetic_customer_id: str
    sizing_system: str
    usual_shoe_size: str
    fit_profile: CustomerFitProfile
    comfort_style_priority: Optional[str]
    occasions: List[str]
    measurements_completed: bool = False
    foot_length_cm: Optional[float] = None
    foot_width_cm: Optional[float] = None


@dataclass(frozen=True)
class ProductCatalogEntry:
    """Catalog attributes for a SKU (Requirement 1.16; design ProductCatalogEntry).

    ``offered_sizes`` is derived as the distinct sizes present for the SKU's *style*
    (design Data Models), so a size recommendation can be clamped to what the style
    actually offers even though each catalog row is a single size.
    """

    sku: str
    found: bool
    category: Optional[str] = None
    price_usd: Optional[float] = None
    occasions: List[str] = field(default_factory=list)
    fit_guidance: Optional[str] = None
    fit_return_rate: Optional[float] = None
    toe_box: Optional[str] = None
    offered_sizes: List[str] = field(default_factory=list)
    gender_category: Optional[str] = None


@dataclass(frozen=True)
class SkuFitSignals:
    """OMS fit-return + exchange signals for a SKU (Requirement 1.1; design SkuFitSignals).

    OMS returns are joined with in-store exchanges on ``Original Order Number`` ->
    ``order_number``. ``exchange_direction_hint`` is a net lean (``up`` / ``down`` /
    ``mixed``) derived only from exchanges whose original and exchange SKU sizes are
    both resolvable in the catalog; it is ``None`` when no exchange size change can
    be determined.
    """

    sku: str
    too_big_count: int = 0
    too_small_count: int = 0
    uncomfortable_count: int = 0
    exchange_count: int = 0
    exchange_direction_hint: Optional[str] = None
    fit_total: int = 0
    total_count: int = 0


@dataclass(frozen=True)
class SkuReviewSignals:
    """Review-derived fit/width/occasion signals for a SKU (Requirement 3.1)."""

    sku: str
    review_count: int = 0
    fit_signal_counts: Dict[str, int] = field(default_factory=dict)
    width_signal_counts: Dict[str, int] = field(default_factory=dict)
    occasion_cues: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class QualityReturns:
    """Quality returns + units sold for a SKU (Requirements 5.1, 5.10).

    ``quality_count`` and ``units_sold`` are both computed from the **same OMS SKU
    key** so ``quality_return_rate`` is a valid ratio (Requirement 5.10). ``units_sold``
    is ``SUM(Shipped)`` over the orders dataset for that SKU.
    """

    sku: str
    quality_count: int = 0
    reason_breakdown: Dict[str, int] = field(default_factory=dict)
    units_sold: int = 0

    @property
    def quality_return_rate(self) -> Optional[float]:
        """Quality returns divided by units sold, or ``None`` when no units sold."""
        if self.units_sold <= 0:
            return None
        return self.quality_count / self.units_sold


# ---------------------------------------------------------------------------
# Mappers
# ---------------------------------------------------------------------------


def map_customer_profile(
    store: DataStore, synthetic_customer_id: str
) -> Optional[CustomerProfile]:
    """Map a customer-profile row into a :class:`CustomerProfile`.

    Returns ``None`` when the customer id is unknown. Foot measurements are exposed
    only when ``measurements_completed = 1`` (design Data Models); when the customer
    did not complete measurement both ``foot_length_cm`` and ``foot_width_cm`` are
    ``None`` even if the raw cells happen to carry a value.
    """
    row = store.get_customer(synthetic_customer_id)
    if row is None:
        return None

    fit_profile = CustomerFitProfile(
        too_big=_flag(row.get("fit_shoes_often_too_big")),
        too_small=_flag(row.get("fit_shoes_often_too_small")),
        too_narrow=_flag(row.get("fit_shoes_too_narrow")),
        too_wide=_flag(row.get("fit_shoes_too_wide")),
        tight_toes=_flag(row.get("fit_tight_around_toes")),
        heels_slip=_flag(row.get("fit_heels_slip")),
        no_issues=_flag(row.get("fit_no_fit_issues")),
        not_sure=_flag(row.get("fit_not_sure")),
    )

    completed = _flag(row.get("measurements_completed"))
    foot_length = _float_or_none(row.get("foot_length_cm")) if completed else None
    foot_width = _float_or_none(row.get("foot_width_cm")) if completed else None

    comfort = _clean(row.get("comfort_style_priority")) or None

    return CustomerProfile(
        synthetic_customer_id=_clean(row.get("synthetic_customer_id")),
        sizing_system=_clean(row.get("shoe_sizing_system")),
        usual_shoe_size=_clean(row.get("usual_shoe_size")),
        fit_profile=fit_profile,
        comfort_style_priority=comfort,
        occasions=normalize_profile_occasions(row.get("usual_shopping_occasions")),
        measurements_completed=completed,
        foot_length_cm=foot_length,
        foot_width_cm=foot_width,
    )


def _sorted_sizes(sizes: Sequence[str]) -> List[str]:
    """Return distinct size tokens sorted numerically where possible, else lexically.

    Catalog sizes are numeric strings ("5", "6.5", "10"); sorting them numerically
    gives a sensible offered-range order for the clamp step. Any non-numeric token
    is kept and ordered after the numeric ones so nothing is silently dropped.
    """
    distinct = {s for s in (sizes or []) if s}

    def key(s: str) -> tuple[int, float, str]:
        try:
            return (0, float(s), "")
        except ValueError:
            return (1, 0.0, s)

    return sorted(distinct, key=key)


def map_product_catalog_entry(
    store: DataStore, sku: str
) -> ProductCatalogEntry:
    """Map catalog rows for a SKU into a :class:`ProductCatalogEntry`.

    Returns an entry with ``found=False`` when the SKU is absent from the catalog
    (Requirement 8.6 / tool contract). ``offered_sizes`` is the distinct set of
    sizes present for the SKU's *style* across the whole catalog, not just the rows
    for this exact SKU — each SKU row is a single size, and the design derives the
    offered range from the style (design Data Models).
    """
    sku = _clean(sku)
    rows = store.get_catalog_rows(sku)
    if not rows:
        return ProductCatalogEntry(sku=sku, found=False)

    primary = rows[0]
    style = _clean(primary.get("style"))

    # offered_sizes: distinct sizes across all catalog rows sharing this style.
    if style:
        style_sizes = [
            _clean(r.get("size"))
            for r in store.product_catalog_rows
            if _clean(r.get("style")) == style
        ]
    else:
        style_sizes = [_clean(r.get("size")) for r in rows]

    return ProductCatalogEntry(
        sku=sku,
        found=True,
        category=_clean(primary.get("category")) or None,
        price_usd=_float_or_none(primary.get("price_usd")),
        occasions=normalize_catalog_occasions(primary.get("occasions")),
        fit_guidance=normalize_fit_guidance(primary.get("fit_guidance")),
        fit_return_rate=_float_or_none(primary.get("fit_return_rate")),
        toe_box=_normalize_toe_box(primary.get("toe_box")),
        offered_sizes=_sorted_sizes(style_sizes),
        gender_category=_clean(primary.get("gender_category")) or None,
    )


def _catalog_size_for_sku(store: DataStore, sku: str) -> Optional[float]:
    """Return the numeric catalog size for a SKU, or ``None`` if unresolved."""
    rows = store.get_catalog_rows(sku)
    if not rows:
        return None
    return _float_or_none(rows[0].get("size"))


def map_sku_fit_signals(store: DataStore, sku: str) -> SkuFitSignals:
    """Map OMS fit returns + joined exchanges into :class:`SkuFitSignals`.

    Fit-return reasons ("Too big", "Too small", "Uncomfortable", with quoting
    variants) are counted from the OMS return rows for the SKU. Exchanges are joined
    on the return's ``order_number`` -> the exchange's ``Original Order Number``;
    ``exchange_count`` is the number of matched exchange rows.

    ``exchange_direction_hint`` is derived only from exchanges whose original SKU
    (the returned SKU) and ``Exchange SKU`` both resolve to a numeric catalog size:
    a larger exchange size means the customer sized up, a smaller size means they
    sized down. The net lean is ``up`` / ``down`` / ``mixed``; ``None`` when no
    exchange size change is resolvable (common with the synthetic exchange data).
    """
    sku = _clean(sku)
    returns = store.get_oms_returns(sku)

    too_big = too_small = uncomfortable = 0
    exchange_count = 0
    up = down = 0

    orig_size = _catalog_size_for_sku(store, sku)

    for ret in returns:
        label = classify_fit_reason(ret.get("reason"))
        if label == "too_big":
            too_big += 1
        elif label == "too_small":
            too_small += 1
        elif label == "uncomfortable":
            uncomfortable += 1

        order_number = _clean(ret.get("order_number"))
        if not order_number:
            continue
        exchanges = store.get_exchanges(order_number)
        for ex in exchanges:
            exchange_count += 1
            ex_sku = _clean(ex.get("exchange sku"))
            ex_size = _catalog_size_for_sku(store, ex_sku) if ex_sku else None
            if orig_size is not None and ex_size is not None:
                if ex_size > orig_size:
                    up += 1
                elif ex_size < orig_size:
                    down += 1

    direction: Optional[str] = None
    if up or down:
        if up and not down:
            direction = "up"
        elif down and not up:
            direction = "down"
        else:
            direction = "mixed"

    fit_total = too_big + too_small + uncomfortable
    return SkuFitSignals(
        sku=sku,
        too_big_count=too_big,
        too_small_count=too_small,
        uncomfortable_count=uncomfortable,
        exchange_count=exchange_count,
        exchange_direction_hint=direction,
        fit_total=fit_total,
        total_count=fit_total + exchange_count,
    )


# Review feedback vocabularies (design SkuReviewSignals).
_REVIEW_FIT_VALUES = ("runs_small", "runs_large", "true_to_size")
_REVIEW_WIDTH_VALUES = ("normal", "narrow", "wide")


def map_sku_review_signals(store: DataStore, sku: str) -> SkuReviewSignals:
    """Map review rows for a SKU into :class:`SkuReviewSignals` (Requirement 3.1).

    ``fit_signal_counts`` / ``width_signal_counts`` tally the canonical
    ``fit_feedback`` and ``width_feedback`` values; ``occasion_cues`` is the
    de-duplicated set of normalized review occasions (first-seen order).
    """
    sku = _clean(sku)
    reviews = store.get_reviews(sku)

    fit_counts: Counter[str] = Counter()
    width_counts: Counter[str] = Counter()
    cues: Dict[str, None] = {}

    for rv in reviews:
        fit = _clean(rv.get("fit_feedback")).lower()
        if fit in _REVIEW_FIT_VALUES:
            fit_counts[fit] += 1

        width = _clean(rv.get("width_feedback")).lower()
        if width in _REVIEW_WIDTH_VALUES:
            width_counts[width] += 1

        cue = normalize_review_occasion(rv.get("occasion"))
        if cue is not None and cue not in cues:
            cues[cue] = None

    return SkuReviewSignals(
        sku=sku,
        review_count=len(reviews),
        fit_signal_counts={k: fit_counts[k] for k in _REVIEW_FIT_VALUES if fit_counts[k]},
        width_signal_counts={
            k: width_counts[k] for k in _REVIEW_WIDTH_VALUES if width_counts[k]
        },
        occasion_cues=list(cues),
    )


def map_quality_returns(store: DataStore, sku: str) -> QualityReturns:
    """Map quality returns + units sold for a SKU into :class:`QualityReturns`.

    ``quality_count`` and the per-reason breakdown come from the OMS return rows for
    the SKU, counting only the four whitelisted quality reasons (Requirement 5.2).
    ``units_sold`` is ``SUM(Shipped)`` over the orders dataset for the **same OMS SKU
    key** (Requirement 5.10), so the derived ``quality_return_rate`` is a valid ratio.
    """
    sku = _clean(sku)

    breakdown: Counter[str] = Counter()
    for ret in store.get_oms_returns(sku):
        label = classify_quality_reason(ret.get("reason"))
        if label is not None:
            breakdown[label] += 1
    quality_count = sum(breakdown.values())

    units_sold = sum(_int_or_zero(order.get("shipped")) for order in store.get_orders(sku))

    return QualityReturns(
        sku=sku,
        quality_count=quality_count,
        reason_breakdown=dict(breakdown),
        units_sold=units_sold,
    )


def map_all_quality_returns(store: DataStore) -> List[QualityReturns]:
    """Map :class:`QualityReturns` for every SKU that has OMS returns or orders.

    The Quality_Issue_Report (Requirement 5.1) needs quality counts + units sold
    across all SKUs; this iterates the union of SKUs seen in the OMS returns and the
    orders datasets so a SKU with sales but no returns (and vice versa) still gets a
    record. Both numerator and denominator stay on the same OMS SKU key
    (Requirement 5.10).
    """
    skus = set(store.oms_returns_by_sku) | set(store.orders_by_sku)
    return [map_quality_returns(store, sku) for sku in sorted(skus)]
