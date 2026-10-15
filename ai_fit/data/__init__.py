"""Data layer: CSV loader + in-memory store.

Loads the six CSV datasets into an in-memory store keyed for fast SKU and customer
lookup. Protected attributes (age, gender, ethnicity, nationality, location) are
stripped at the source so they are never materialized into model-visible records
(Requirements 10.2, 10.6). Legitimate attributes — the customer ``sizing_system``
and the product ``gender_category`` — are retained (Requirements 10.5, 10.7).

The raw loader and index live in :mod:`ai_fit.data.store`. Mapping raw CSV columns
into the design's typed records lives in :mod:`ai_fit.data.mappers` (Task 2.2) and
builds on this store.
"""

from ai_fit.data.mappers import (
    CustomerFitProfile,
    CustomerProfile,
    ProductCatalogEntry,
    QualityReturns,
    SkuFitSignals,
    SkuReviewSignals,
    map_all_quality_returns,
    map_customer_profile,
    map_product_catalog_entry,
    map_quality_returns,
    map_sku_fit_signals,
    map_sku_review_signals,
    normalize_fit_guidance,
)
from ai_fit.data.store import DataStore, is_protected_column

__all__ = [
    "DataStore",
    "is_protected_column",
    "CustomerFitProfile",
    "CustomerProfile",
    "ProductCatalogEntry",
    "QualityReturns",
    "SkuFitSignals",
    "SkuReviewSignals",
    "map_all_quality_returns",
    "map_customer_profile",
    "map_product_catalog_entry",
    "map_quality_returns",
    "map_sku_fit_signals",
    "map_sku_review_signals",
    "normalize_fit_guidance",
]
