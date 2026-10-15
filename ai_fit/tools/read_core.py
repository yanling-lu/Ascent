"""Core read Agent Tools: ``get_sku_fit_signals``, ``get_customer_profile``,
``get_product_catalog_entry`` (Task 7.1).

Each function is a thin wrapper over a Task 2.2 mapper in :mod:`ai_fit.data.mappers`.
The mappers already load the correct fields from the protected-attribute-free store
and build the design's typed records; these tools *project* those records into the
plain, JSON-serializable ``dict`` structures Claude reasons over through the Bedrock
Converse ``toolConfig``, matching the exact output shapes in the design's Agent Tool
Catalog (design "Agent Tool Catalog").

Why a projection layer rather than returning the dataclasses directly:
* The catalog output shapes differ in a few places from the internal records — e.g.
  the fit-signals tool exposes ``total_signal_count`` (not the record's bookkeeping
  ``total_count`` / ``fit_total``), and the customer-profile tool omits the record's
  ``synthetic_customer_id`` and ``measurements_completed`` and only emits the foot
  measurement keys when they are present. Projecting here keeps the model-visible
  contract exactly as the design specifies and decoupled from internal record fields.
* The returned structures are guaranteed JSON-serializable (plain ``dict`` / ``list``
  / ``str`` / ``int`` / ``float`` / ``bool`` / ``None``), which the dispatcher,
  validator, and trace all consume.

Protected attributes (age, gender, ethnicity, nationality, location) are stripped at
the data layer (Requirements 10.2, 10.6) and are therefore never present in any
record these tools project. ``sizing_system`` (Requirement 10.5) and product
``gender_category`` (Requirement 10.7) are retained legitimate attributes and are
surfaced here.

Design references:
* Agent Tool Catalog rows #1 ``get_sku_fit_signals``, #2 ``get_customer_profile``,
  #3 ``get_product_catalog_entry``.
* Requirements 1.1, 1.3, 1.5-1.7, 1.10, 1.11, 1.16, 2.1, 2.2, 8.1, 8.5, 8.6, 10.1, 10.7.
"""

from __future__ import annotations

from typing import Any, Dict

from ai_fit.data.mappers import (
    CustomerProfile,
    ProductCatalogEntry,
    SkuFitSignals,
    map_customer_profile,
    map_product_catalog_entry,
    map_sku_fit_signals,
)
from ai_fit.data.store import DataStore

# A tool result is always a plain JSON-serializable object.
ToolResult = Dict[str, Any]


# ---------------------------------------------------------------------------
# Tool #1 — get_sku_fit_signals
# ---------------------------------------------------------------------------


def get_sku_fit_signals(store: DataStore, sku: str) -> ToolResult:
    """Return OMS fit-return + exchange signals for a SKU (design tool #1).

    Output shape (design Agent Tool Catalog)::

        {
            "too_big_count": int,
            "too_small_count": int,
            "uncomfortable_count": int,
            "exchange_count": int,
            "total_signal_count": int,
            "exchange_direction_hint": "up" | "down" | "mixed" | null,
        }

    ``total_signal_count`` is the combined count of fit-return and matched-exchange
    signals the model weighs for the OMS lean (Requirements 1.1, 1.3, 1.5-1.7, 1.10).
    A SKU with no OMS returns and no exchanges yields all-zero counts and a ``null``
    direction hint, which the model treats as a sparse-OMS SKU rather than an error.
    """
    signals: SkuFitSignals = map_sku_fit_signals(store, sku)
    return {
        "too_big_count": signals.too_big_count,
        "too_small_count": signals.too_small_count,
        "uncomfortable_count": signals.uncomfortable_count,
        "exchange_count": signals.exchange_count,
        "total_signal_count": signals.total_count,
        "exchange_direction_hint": signals.exchange_direction_hint,
    }


# ---------------------------------------------------------------------------
# Tool #2 — get_customer_profile
# ---------------------------------------------------------------------------


def get_customer_profile(store: DataStore, synthetic_customer_id: str) -> ToolResult:
    """Return the customer fit profile (design tool #2).

    Output shape (design Agent Tool Catalog)::

        {
            "sizing_system": str,
            "usual_shoe_size": str,
            "fit_profile": {
                "too_big": bool, "too_small": bool, "too_narrow": bool,
                "too_wide": bool, "tight_toes": bool, "heels_slip": bool,
                "no_issues": bool, "not_sure": bool,
            },
            "comfort_style_priority": str | null,
            "occasions": [str, ...],
            "foot_length_cm"?: float,   # present only when measured
            "foot_width_cm"?: float,    # present only when measured
            "found": bool,
        }

    Protected attributes are excluded at the source (Requirements 10.1, 10.6); the
    retained legitimate ``sizing_system`` (Requirement 10.5) and the normalized
    ``occasions`` list are surfaced. Foot measurement keys are emitted **only** when
    the customer completed measurement (the mapper returns ``None`` otherwise), so a
    non-measured profile simply omits them rather than carrying ``null`` values.

    An unknown ``synthetic_customer_id`` returns ``{"found": false}`` so the caller
    can distinguish "no such customer" from a legitimately sparse profile
    (Requirements 1.11, 2.1, 8.1). A known customer always carries ``"found": true``.
    """
    profile = map_customer_profile(store, synthetic_customer_id)
    if profile is None:
        return {"found": False}

    result: ToolResult = _project_customer_profile(profile)
    result["found"] = True
    return result


def _project_customer_profile(profile: CustomerProfile) -> ToolResult:
    """Project a :class:`CustomerProfile` into the catalog's output ``dict``.

    Emits the foot measurement keys only when present so the shape matches the
    design's optional ``foot_length_cm?`` / ``foot_width_cm?`` fields. Does not emit
    the internal ``synthetic_customer_id`` or ``measurements_completed`` fields,
    which are not part of the model-visible contract.
    """
    fit = profile.fit_profile
    result: ToolResult = {
        "sizing_system": profile.sizing_system,
        "usual_shoe_size": profile.usual_shoe_size,
        "fit_profile": {
            "too_big": fit.too_big,
            "too_small": fit.too_small,
            "too_narrow": fit.too_narrow,
            "too_wide": fit.too_wide,
            "tight_toes": fit.tight_toes,
            "heels_slip": fit.heels_slip,
            "no_issues": fit.no_issues,
            "not_sure": fit.not_sure,
        },
        "comfort_style_priority": profile.comfort_style_priority,
        "occasions": list(profile.occasions),
    }
    if profile.foot_length_cm is not None:
        result["foot_length_cm"] = profile.foot_length_cm
    if profile.foot_width_cm is not None:
        result["foot_width_cm"] = profile.foot_width_cm
    return result


# ---------------------------------------------------------------------------
# Tool #3 — get_product_catalog_entry
# ---------------------------------------------------------------------------


def get_product_catalog_entry(store: DataStore, sku: str) -> ToolResult:
    """Return catalog attributes for a SKU (design tool #3).

    Output shape (design Agent Tool Catalog)::

        {
            "found": bool,
            "category": str | null,
            "price_usd": float | null,
            "occasions": [str, ...],
            "fit_guidance": "runs small" | "runs large" | "true to size" | null,
            "fit_return_rate": float | null,
            "toe_box": "regular" | "narrow" | "roomy" | null,
            "offered_sizes": [str, ...],
            "gender_category"?: str,
        }

    An absent SKU returns ``{"found": false}`` with no attribute keys
    (Requirements 8.5, 8.6) so the caller never mistakes a missing SKU for one with
    empty attributes. For a present SKU, ``found`` is ``true`` and ``offered_sizes``
    is the distinct size range of the SKU's style (Requirements 1.16, 2.2). The
    retained ``gender_category`` (Requirement 10.7) is emitted only when present.
    """
    entry: ProductCatalogEntry = map_product_catalog_entry(store, sku)
    if not entry.found:
        return {"found": False}

    result: ToolResult = {
        "found": True,
        "category": entry.category,
        "price_usd": entry.price_usd,
        "occasions": list(entry.occasions),
        "fit_guidance": entry.fit_guidance,
        "fit_return_rate": entry.fit_return_rate,
        "toe_box": entry.toe_box,
        "offered_sizes": list(entry.offered_sizes),
    }
    if entry.gender_category is not None:
        result["gender_category"] = entry.gender_category
    return result


__all__ = [
    "get_sku_fit_signals",
    "get_customer_profile",
    "get_product_catalog_entry",
]
