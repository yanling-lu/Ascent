"""CSV loader and in-memory store for the AI Fit data layer.

This module implements Task 2.1: it loads the six CSV datasets into an in-memory
store, builds SKU-keyed and customer-keyed indexes for fast lookup, and strips
protected attributes (age, gender, ethnicity, nationality, location) **at the
source** so they are never materialized into any model-visible record
(Requirements 10.2, 10.6).

Scope note: this module is deliberately a *raw* loader + index. It reads each CSV
into lists of plain ``dict`` rows (keyed by the CSV's own column headers) and
indexes them. It does **not** map raw columns into the design's typed records
(``CustomerProfile``, ``ProductCatalogEntry``, ``SkuFitSignals`` …) — that is
Task 2.2, built on top of this store.

Retained legitimate attributes (Requirements 10.5, 10.7): the customer
``sizing_system`` (CSV column ``shoe_sizing_system``) and the product
``gender_category`` are explicitly *not* treated as protected and pass through
untouched.

The store performs the only I/O in the data layer at construction time; after
``DataStore.load()`` every lookup is a pure in-memory dict access.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Sequence

from ai_fit import config

# ---------------------------------------------------------------------------
# Protected-attribute stripping (Requirements 10.2, 10.6)
# ---------------------------------------------------------------------------

# Column-name fragments that identify a protected attribute. Any CSV column whose
# normalized header *contains* one of these fragments is dropped at load time, so
# a protected value can never reach an index, a tool, or the model.
#
# These are matched case-insensitively against the normalized column name (see
# ``_normalize_header``). The list is intentionally broad: the current synthetic
# datasets happen not to carry age/gender/ethnicity/nationality/location columns,
# but stripping by name means a future column added to any CSV is excluded by
# default rather than silently leaking.
_PROTECTED_FRAGMENTS: tuple[str, ...] = (
    "age",
    "birth",  # date_of_birth, birthdate, birth_year
    "dob",
    "gender",
    "sex",
    "ethnicity",
    "ethnic",
    "race",
    "nationality",
    "citizenship",
    "country",
    "location",
    "address",
    "city",
    "state",
    "province",
    "zip",
    "postal",
    "region",
    "latitude",
    "longitude",
)

# Legitimate attributes that must survive stripping even though their names would
# otherwise collide with a protected fragment. ``shoe_sizing_system`` is retained
# (Requirement 10.5) and the product ``gender_category`` is retained
# (Requirement 10.7) — the latter overlaps the ``gender`` fragment, so it is
# whitelisted explicitly. Matched against the normalized header.
_RETAINED_HEADERS: frozenset[str] = frozenset(
    {
        "shoe_sizing_system",
        "sizing_system",
        "gender_category",
    }
)


def _normalize_header(header: str) -> str:
    """Lower-case and trim a CSV header for consistent matching.

    Quoting and surrounding whitespace in the raw CSV headers (several of the OMS
    files quote every field) are removed so fragment matching is reliable.
    """
    return header.strip().strip('"').strip().lower()


def is_protected_column(header: str) -> bool:
    """Return ``True`` if a CSV column should be stripped as a protected attribute.

    A column is protected when its normalized name contains any protected fragment
    *and* is not on the retained-attribute whitelist. Pure and side-effect free so
    it can be unit-tested directly.
    """
    normalized = _normalize_header(header)
    if normalized in _RETAINED_HEADERS:
        return False
    return any(fragment in normalized for fragment in _PROTECTED_FRAGMENTS)


def _strip_protected(row: Mapping[str, str]) -> Dict[str, str]:
    """Return a copy of ``row`` with every protected column removed.

    Keys are normalized headers; this is applied to every row of every dataset as
    it is read, so protected values are never stored anywhere in the index.
    """
    return {
        _normalize_header(key): value
        for key, value in row.items()
        if key is not None and not is_protected_column(key)
    }


# ---------------------------------------------------------------------------
# Low-level CSV reader
# ---------------------------------------------------------------------------


def _read_csv(path: Path) -> List[Dict[str, str]]:
    """Read a CSV into a list of protected-attribute-free row dicts.

    Uses ``csv.DictReader`` so quoting (the OMS files quote every field) is handled
    correctly. Headers are normalized and protected columns are dropped per row.
    ``utf-8-sig`` tolerates a BOM if one is present.
    """
    rows: List[Dict[str, str]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            rows.append(_strip_protected(raw))
    return rows


def _index_by(rows: Sequence[Mapping[str, str]], key: str) -> Dict[str, List[dict]]:
    """Group rows into a dict mapping a key column's value to the list of rows.

    Rows missing the key or carrying an empty key value are skipped. Keys are
    stored as stripped strings so lookups are stable regardless of surrounding
    whitespace in the source data.

    The index stores **references** to the same row dicts held in the owning
    ``*_rows`` list rather than copies — at ~1M-row dataset sizes copying every
    row again would double load time and memory. The rows are only read by the
    data layer and the Task 2.2 mappers, which never mutate them in place.
    """
    index: Dict[str, List[dict]] = {}
    for row in rows:
        value = row.get(key)
        if value is None:
            continue
        value = str(value).strip()
        if not value:
            continue
        index.setdefault(value, []).append(row)
    return index


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------


@dataclass
class DataStore:
    """In-memory store of the six datasets with SKU- and customer-keyed indexes.

    All attributes hold raw (protected-attribute-free) rows keyed by the CSV's own
    normalized column names. Construct via :meth:`load`, which performs the CSV
    I/O once; thereafter every lookup is an in-memory dict access.

    Index layout:

    * ``customers_by_id`` — one profile row per ``synthetic_customer_id``.
    * ``catalog_by_sku`` — catalog rows grouped by ``sku`` (a SKU can appear once
      per offered size, so the value is a list).
    * ``oms_returns_by_sku`` — OMS return rows grouped by ``sku``.
    * ``orders_by_sku`` / ``orders_by_order_number`` — order rows grouped two ways;
      the order-number index supports the Task 2.2 exchange join.
    * ``exchanges_by_order_number`` — in-store exchange rows grouped by
      ``original order number`` (the join key back to orders).
    * ``reviews_by_sku`` — review rows grouped by ``sku``.

    The raw ``*_rows`` lists are retained so Task 2.2 mappers can iterate the full
    dataset without re-reading the CSVs.
    """

    customer_profiles_rows: List[Dict[str, str]] = field(default_factory=list)
    product_catalog_rows: List[Dict[str, str]] = field(default_factory=list)
    oms_returns_rows: List[Dict[str, str]] = field(default_factory=list)
    oms_orders_rows: List[Dict[str, str]] = field(default_factory=list)
    instore_exchanges_rows: List[Dict[str, str]] = field(default_factory=list)
    customer_reviews_rows: List[Dict[str, str]] = field(default_factory=list)

    # Indexes (built from the rows above).
    customers_by_id: Dict[str, Dict[str, str]] = field(default_factory=dict)
    catalog_by_sku: Dict[str, List[dict]] = field(default_factory=dict)
    oms_returns_by_sku: Dict[str, List[dict]] = field(default_factory=dict)
    orders_by_sku: Dict[str, List[dict]] = field(default_factory=dict)
    orders_by_order_number: Dict[str, List[dict]] = field(default_factory=dict)
    exchanges_by_order_number: Dict[str, List[dict]] = field(default_factory=dict)
    reviews_by_sku: Dict[str, List[dict]] = field(default_factory=dict)

    # -- construction -------------------------------------------------------

    @classmethod
    def load(cls, dataset_paths: Mapping[str, Path] | None = None) -> "DataStore":
        """Load all six datasets from ``dataset_paths`` and build the indexes.

        Defaults to :data:`ai_fit.config.DATASET_PATHS`. Protected attributes are
        stripped as each row is read. Raises ``FileNotFoundError`` if a configured
        dataset is missing so callers fail fast rather than silently serving an
        empty index.
        """
        paths = dict(dataset_paths or config.DATASET_PATHS)

        missing = [name for name, p in paths.items() if not Path(p).is_file()]
        if missing:
            raise FileNotFoundError(
                "missing dataset CSV(s): " + ", ".join(sorted(missing))
            )

        store = cls(
            customer_profiles_rows=_read_csv(Path(paths["customer_profiles"])),
            product_catalog_rows=_read_csv(Path(paths["product_catalog"])),
            oms_returns_rows=_read_csv(Path(paths["oms_returns"])),
            oms_orders_rows=_read_csv(Path(paths["oms_orders"])),
            instore_exchanges_rows=_read_csv(Path(paths["instore_exchanges"])),
            customer_reviews_rows=_read_csv(Path(paths["customer_reviews"])),
        )
        store._build_indexes()
        return store

    def _build_indexes(self) -> None:
        """Populate every index from the loaded raw rows.

        Called once by :meth:`load`. The customer index keeps the last row seen for
        a given id (ids are unique in the profile dataset); all other indexes group
        rows into lists because a SKU or order number can legitimately recur.
        """
        self.customers_by_id = {
            str(row["synthetic_customer_id"]).strip(): row
            for row in self.customer_profiles_rows
            if row.get("synthetic_customer_id")
        }

        self.catalog_by_sku = _index_by(self.product_catalog_rows, "sku")
        self.oms_returns_by_sku = _index_by(self.oms_returns_rows, "sku")
        self.orders_by_sku = _index_by(self.oms_orders_rows, "sku")
        self.orders_by_order_number = _index_by(self.oms_orders_rows, "order_number")
        self.exchanges_by_order_number = _index_by(
            self.instore_exchanges_rows, "original order number"
        )
        self.reviews_by_sku = _index_by(self.customer_reviews_rows, "sku")

    # -- lookups ------------------------------------------------------------

    def get_customer(self, synthetic_customer_id: str) -> Dict[str, str] | None:
        """Return the raw profile row for a customer id, or ``None`` if absent."""
        return self.customers_by_id.get(str(synthetic_customer_id).strip())

    def get_catalog_rows(self, sku: str) -> List[dict]:
        """Return catalog rows for a SKU (empty list if the SKU is absent)."""
        return self.catalog_by_sku.get(str(sku).strip(), [])

    def get_oms_returns(self, sku: str) -> List[dict]:
        """Return OMS return rows for a SKU (empty list if none)."""
        return self.oms_returns_by_sku.get(str(sku).strip(), [])

    def get_orders(self, sku: str) -> List[dict]:
        """Return order rows for a SKU (empty list if none)."""
        return self.orders_by_sku.get(str(sku).strip(), [])

    def get_orders_by_number(self, order_number: str) -> List[dict]:
        """Return order rows for an order number (empty list if none)."""
        return self.orders_by_order_number.get(str(order_number).strip(), [])

    def get_exchanges(self, original_order_number: str) -> List[dict]:
        """Return in-store exchange rows for an original order number."""
        return self.exchanges_by_order_number.get(
            str(original_order_number).strip(), []
        )

    def get_reviews(self, sku: str) -> List[dict]:
        """Return review rows for a SKU (empty list if none)."""
        return self.reviews_by_sku.get(str(sku).strip(), [])
