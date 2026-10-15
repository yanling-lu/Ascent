"""Occasion matching and the ``find_alternative_styles`` Agent Tool (Task 8.8).

This module implements two related pieces of the design:

* :func:`match_occasion` — the Occasion Matching logic (Requirement 2 and the
  review-cue enrichment of Requirement 3.5). It compares the customer's canonical
  occasions against a SKU's catalog occasions (optionally enriched by review
  occasion cues) as a **set intersection**, naming each matching occasion
  (Requirements 2.3, 2.4), and models the direct-occasion override, the
  no-occasion-available, the retrieval-failure, and the unrecognized-value cases
  (Requirements 2.5-2.8).

* :func:`find_alternative_styles` — Agent Tool catalog entry #10. When the browsed
  SKU does not match the requested occasion, it returns catalog-present (available,
  per the design's availability proxy) alternative SKUs whose suited occasions
  include the requested occasion, ranked by closeness to the browsed SKU's category
  then by smallest absolute price difference, capped at the top three
  (Requirements 6.1-6.5).

Both functions are pure reads over an already-loaded
:class:`~ai_fit.data.store.DataStore`: they reuse the Occasion Normalization Layer
(:mod:`ai_fit.reasoning.occasion`) and the Task 2.2 mappers
(:func:`~ai_fit.data.mappers.map_product_catalog_entry`,
:func:`~ai_fit.data.mappers.map_sku_review_signals`) rather than re-reading or
re-normalizing raw CSV columns, so the canonical occasion set and the catalog
shapes stay consistent with the rest of the system.

Availability proxy (design "Open Item — Stock / Inventory"): no dataset carries a
stock column, so **a SKU present in the catalog is treated as available**. Real-time
inventory is future work; the ``in stock`` language in Requirements 6.1-6.3 and 6.5
is satisfied here by catalog presence.

Design references:
* Occasion Normalization Layer; Agent Tool Catalog entry #10 ``find_alternative_styles``.
* Requirements 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 3.5, 6.1, 6.2, 6.3, 6.4, 6.5.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

from ai_fit.data.mappers import (
    ProductCatalogEntry,
    map_product_catalog_entry,
    map_sku_review_signals,
)
from ai_fit.data.store import DataStore
from ai_fit.reasoning.occasion import CANONICAL_OCCASIONS

# A tool result / match result is always a plain JSON-serializable object.
ToolResult = Dict[str, Any]

#: The number of alternative styles presented at most (Requirement 6.2).
MAX_ALTERNATIVES = 3


# ---------------------------------------------------------------------------
# Occasion matching (Requirements 2.3-2.8, 3.5)
# ---------------------------------------------------------------------------


def _canonical_subset(values: Optional[Iterable[str]]) -> List[str]:
    """Return the already-canonical occasion values from ``values``, de-duplicated.

    The customer and catalog occasion lists coming from the mappers are already
    normalized to the canonical set, so this simply keeps recognized values in
    first-seen order and drops anything outside the canonical set defensively.
    """
    seen: Dict[str, None] = {}
    for value in values or []:
        if value in CANONICAL_OCCASIONS and value not in seen:
            seen[value] = None
    return list(seen)


def partition_requested_occasions(
    requested: Optional[Iterable[str]],
) -> tuple[List[str], List[str]]:
    """Split caller-supplied occasion values into (recognized, unrecognized).

    Recognized values are canonical occasions (de-duplicated, first-seen order);
    unrecognized values are any token not in the canonical set — surfaced so the
    caller can tell the customer which values were not recognized (Requirement 2.8).
    Empty / ``None`` tokens are ignored entirely rather than reported as
    unrecognized.
    """
    recognized: Dict[str, None] = {}
    unrecognized: Dict[str, None] = {}
    for raw in requested or []:
        if raw is None:
            continue
        value = str(raw).strip()
        if not value:
            continue
        if value in CANONICAL_OCCASIONS:
            recognized.setdefault(value, None)
        else:
            unrecognized.setdefault(value, None)
    return list(recognized), list(unrecognized)


def match_occasion(
    store: DataStore,
    sku: str,
    *,
    profile_occasions: Optional[Sequence[str]] = None,
    direct_occasions: Optional[Sequence[str]] = None,
    include_review_cues: bool = True,
) -> ToolResult:
    """Determine whether a SKU matches the customer's requested occasion(s).

    The match is the **set intersection** of the customer's canonical occasions and
    the SKU's suited occasions (Requirements 2.3, 2.4). The SKU's suited occasions
    come from the catalog, optionally enriched with normalized review occasion cues
    as supporting evidence (Requirement 3.5); when a match is driven by a cue not in
    the catalog occasions, the contributing cues are surfaced in ``review_cues_used``
    so the explanation can cite them.

    Occasion source precedence (Requirement 2.5): when ``direct_occasions`` is
    supplied with at least one recognized value, those replace the profile
    occasions entirely; otherwise the profile occasions are used.

    Return shape::

        {
          "status": "match" | "no_match" | "needs_occasion"
                    | "occasion_unavailable" | "sku_not_found",
          "matched_occasions": [str, ...],      # named matches (Req 2.3)
          "customer_occasions": [str, ...],     # recognized source occasions used
          "sku_occasions": [str, ...],          # catalog + cue occasions considered
          "review_cues_used": [str, ...],       # cues that contributed (Req 3.5)
          "unrecognized_values": [str, ...],    # rejected tokens (Req 2.8)
          "message": str,
        }

    Status cases:
    * ``sku_not_found`` — the SKU is absent from the catalog (so no suited occasions
      can be retrieved); no match/no-match is reported (Requirement 2.7 / 8.6).
    * ``occasion_unavailable`` — the catalog carries no occasion data for the SKU, so
      occasion suitability cannot be determined; no result is reported
      (Requirements 2.7, 8.5).
    * ``needs_occasion`` — no recognized occasion is available from either a direct
      specification or the profile; the caller must request one and must not report a
      match/no-match (Requirement 2.6).
    * ``match`` / ``no_match`` — the intersection is non-empty / empty
      (Requirements 2.3, 2.4).
    """
    recognized_direct, unrecognized_direct = partition_requested_occasions(
        direct_occasions
    )
    recognized_profile, unrecognized_profile = partition_requested_occasions(
        profile_occasions
    )

    # Requirement 2.5: a direct specification (with at least one recognized value)
    # replaces the profile occasions entirely. If a direct spec was given but every
    # value was unrecognized, we still treat the direct values as authoritative so we
    # do not silently fall back to the profile — the unrecognized values are reported
    # and the result is needs_occasion.
    if recognized_direct or unrecognized_direct:
        customer_occasions = recognized_direct
        unrecognized_values = unrecognized_direct
    else:
        customer_occasions = recognized_profile
        unrecognized_values = unrecognized_profile

    # Requirement 2.6: no usable occasion from either source — request one, report
    # no match/no-match result.
    if not customer_occasions:
        return {
            "status": "needs_occasion",
            "matched_occasions": [],
            "customer_occasions": [],
            "sku_occasions": [],
            "review_cues_used": [],
            "unrecognized_values": unrecognized_values,
            "message": (
                "Please provide an occasion (everyday wear, work, or special "
                "occasion) so I can check whether this product suits it."
            ),
        }

    entry: ProductCatalogEntry = map_product_catalog_entry(store, sku)

    # Requirement 2.7 / 8.6: SKU absent from the catalog — suited occasions cannot be
    # retrieved; do not report a match/no-match.
    if not entry.found:
        return {
            "status": "sku_not_found",
            "matched_occasions": [],
            "customer_occasions": customer_occasions,
            "sku_occasions": [],
            "review_cues_used": [],
            "unrecognized_values": unrecognized_values,
            "message": "The product could not be found; please provide a valid SKU.",
        }

    catalog_occasions = _canonical_subset(entry.occasions)

    # Requirement 3.5: review occasion cues are supporting evidence for the SKU's
    # suited occasions. They are already normalized to the canonical set by the
    # mapper; add any not already present as catalog occasions.
    review_cues: List[str] = []
    if include_review_cues:
        cues = _canonical_subset(map_sku_review_signals(store, sku).occasion_cues)
        review_cues = [cue for cue in cues if cue not in catalog_occasions]

    sku_occasions = catalog_occasions + review_cues

    # Requirements 2.7 / 8.5: no occasion data at all for the SKU — cannot determine
    # suitability.
    if not sku_occasions:
        return {
            "status": "occasion_unavailable",
            "matched_occasions": [],
            "customer_occasions": customer_occasions,
            "sku_occasions": [],
            "review_cues_used": [],
            "unrecognized_values": unrecognized_values,
            "message": (
                "Occasion suitability cannot be determined for this product."
            ),
        }

    # Requirements 2.3 / 2.4: the match is the set intersection, preserving the
    # customer's occasion order for a stable, readable naming.
    sku_set = set(sku_occasions)
    matched = [occ for occ in customer_occasions if occ in sku_set]
    cues_used = [cue for cue in review_cues if cue in matched]

    if matched:
        message = "This product matches your occasion: " + ", ".join(matched) + "."
        status = "match"
    else:
        message = "This product does not match your requested occasion."
        status = "no_match"

    return {
        "status": status,
        "matched_occasions": matched,
        "customer_occasions": customer_occasions,
        "sku_occasions": sku_occasions,
        "review_cues_used": cues_used,
        "unrecognized_values": unrecognized_values,
        "message": message,
    }


# ---------------------------------------------------------------------------
# find_alternative_styles — Agent Tool #10 (Requirements 6.1-6.5)
# ---------------------------------------------------------------------------


def _catalog_skus(store: DataStore) -> List[str]:
    """Return every catalog SKU key (the available universe under the proxy).

    A SKU present in the catalog is treated as available (design availability
    proxy), so the catalog's SKU index is the pool of candidate alternatives.
    """
    return list(store.catalog_by_sku.keys())


def _rank_key(
    candidate: ProductCatalogEntry, browsed: ProductCatalogEntry
) -> tuple[int, float, str]:
    """Rank key for an alternative: category closeness, then price distance.

    Ordered tuple (smaller sorts earlier), per Requirement 6.2:

    1. ``category_rank`` — ``0`` when the candidate's category equals the browsed
       SKU's category (closest match), ``1`` otherwise. The catalog has no category
       hierarchy to measure partial closeness, so "closest" is modeled as the
       same-category group first, everything else after.
    2. ``price_distance`` — the absolute price difference from the browsed SKU.
       A missing price on either side sorts last within its category group (treated
       as the largest possible distance) rather than being discarded.
    3. ``sku`` — a final tie-break on the SKU key for a stable, deterministic order.
    """
    same_category = (
        candidate.category is not None
        and browsed.category is not None
        and candidate.category == browsed.category
    )
    category_rank = 0 if same_category else 1

    if candidate.price_usd is not None and browsed.price_usd is not None:
        price_distance = abs(candidate.price_usd - browsed.price_usd)
    else:
        price_distance = float("inf")

    return (category_rank, price_distance, candidate.sku)


def _alternative_payload(entry: ProductCatalogEntry) -> ToolResult:
    """Project a candidate :class:`ProductCatalogEntry` into the tool #10 shape.

    Output per the design tool contract: ``{sku, category, price_usd, occasions[]}``.
    """
    return {
        "sku": entry.sku,
        "category": entry.category,
        "price_usd": entry.price_usd,
        "occasions": list(entry.occasions),
    }


def find_alternative_styles(
    store: DataStore, requested_occasion: str, browsed_sku: str
) -> ToolResult:
    """Return occasion-matching alternative styles for a browsed SKU (tool #10).

    Inputs (design Agent Tool Catalog #10): ``{requested_occasion, browsed_sku}``.
    Scans the catalog-present (available) SKUs for those whose suited occasions
    include ``requested_occasion`` (Requirement 6.1), excluding the browsed SKU
    itself, ranks them by closeness to the browsed SKU's category then by smallest
    absolute price difference (Requirement 6.2), and caps the result at the top
    three. When 1-3 candidates exist, all are presented (Requirement 6.3); when more
    than three exist, only the top three ranked are returned (Requirement 6.2).

    Return shape::

        {
          "requested_occasion": str,            # echoed; satisfied by each result (Req 6.4)
          "alternatives": [                     # 0-3 entries, ranked
              {"sku": str, "category": str|null,
               "price_usd": float|null, "occasions": [str, ...]},
              ...
          ],
          "none_available": bool,               # True in the empty case (Req 6.5)
          "message": str,
        }

    Requirement 6.5 (none available): if no catalog-present SKU's suited occasions
    include the requested occasion, ``alternatives`` is empty, ``none_available`` is
    ``True``, and the message states that none is currently available.

    If ``requested_occasion`` is not a canonical occasion, no SKU can match it, so
    the function returns the empty / none-available result (the occasion is validated
    upstream by :func:`match_occasion`; this guards the tool defensively).
    """
    occasion = str(requested_occasion).strip() if requested_occasion else ""
    browsed = map_product_catalog_entry(store, browsed_sku)
    browsed_key = browsed.sku if browsed.found else str(browsed_sku).strip()

    none_message = (
        "No matching alternative style is currently available for the "
        f"requested occasion ({occasion})."
        if occasion
        else "No matching alternative style is currently available."
    )

    # A non-canonical requested occasion can never be a SKU's suited occasion.
    if occasion not in CANONICAL_OCCASIONS:
        return {
            "requested_occasion": occasion,
            "alternatives": [],
            "none_available": True,
            "message": none_message,
        }

    candidates: List[ProductCatalogEntry] = []
    for sku in _catalog_skus(store):
        if sku == browsed_key:
            continue  # never recommend the browsed SKU back to the customer
        entry = map_product_catalog_entry(store, sku)
        if not entry.found:
            continue
        if occasion in set(entry.occasions):
            candidates.append(entry)

    # Requirement 6.5: explicit none-available case.
    if not candidates:
        return {
            "requested_occasion": occasion,
            "alternatives": [],
            "none_available": True,
            "message": none_message,
        }

    # Requirement 6.2: rank by category closeness, then smallest price difference.
    ranked = sorted(candidates, key=lambda entry: _rank_key(entry, browsed))

    # Requirement 6.2 / 6.3: cap at the top three; present all when 1-3 exist.
    top = ranked[:MAX_ALTERNATIVES]

    return {
        "requested_occasion": occasion,
        "alternatives": [_alternative_payload(entry) for entry in top],
        "none_available": False,
        "message": (
            f"Found {len(top)} alternative style(s) that suit {occasion}."
        ),
    }


__all__ = [
    "MAX_ALTERNATIVES",
    "match_occasion",
    "find_alternative_styles",
    "partition_requested_occasions",
]
