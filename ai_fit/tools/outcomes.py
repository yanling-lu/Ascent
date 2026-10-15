"""Recommendation-outcome write path and in-memory outcome store (Task 8.1).

This module implements the learning-loop write side of Requirement 4:

* :func:`record_recommendation_outcome` — Agent_Tool #7 in the design's tool
  catalog. It writes an **initial ``unknown`` outcome** that links a size
  recommendation to the SKU, the recommended size, and the order, and returns
  ``{outcome_id, status: "unknown"}`` (Requirement 4.1).
* :class:`OutcomeStore` — an in-memory store holding the written
  :class:`RecommendationOutcome` records, keyed for fast SKU lookup (so the read
  tool ``get_recommendation_outcomes`` can aggregate them) and for fast
  outcome-id lookup (so a later return/exchange can update the record).
* :meth:`OutcomeStore.update_outcome` — moves a record from ``unknown`` to one of
  the terminal statuses (``kept`` / ``returned too big`` / ``returned too small``
  / ``returned other``) when a return or exchange becomes known (Requirement 4.2).
  Updates require **no human approval** and are immediately available to the next
  recommendation for the same SKU (Requirement 4.5).

Privacy (Requirement 4.7): the raw ``order_number`` is **never stored**. It is
hashed to an ``order_number_hash`` on write so an individual customer cannot be
re-identified from a :class:`RecommendationOutcome`. The hash is a one-way SHA-256
digest; the same order consistently maps to the same hash so a later return/exchange
for that order can be matched back to the outcome without ever retaining the raw
identifier.

Design references:
* Data Models — ``RecommendationOutcome`` (``outcome_id, sku, recommended_size,
  order_number_hash, status, created_at, updated_at``; identifiers masked).
* Agent Tool Catalog entry #7 ``record_recommendation_outcome``.
* Requirements 4.1, 4.2, 4.5, 4.7.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Dict, List, Mapping, Optional

# ---------------------------------------------------------------------------
# Outcome status vocabulary (Requirement 4.1, 4.2; Glossary Recommendation_Outcome)
# ---------------------------------------------------------------------------

# The initial status every outcome is written with (Requirement 4.1).
STATUS_UNKNOWN = "unknown"

# The terminal statuses an outcome can be updated to once a return/exchange is
# known (Requirement 4.2). "kept" means no fit-driven return occurred; the three
# "returned …" statuses mirror the fit-return reason that resolved the order.
STATUS_KEPT = "kept"
STATUS_RETURNED_TOO_BIG = "returned too big"
STATUS_RETURNED_TOO_SMALL = "returned too small"
STATUS_RETURNED_OTHER = "returned other"

# The full set of statuses an outcome may be updated to. ``unknown`` is excluded:
# a record starts there and only ever moves forward to a terminal status.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        STATUS_KEPT,
        STATUS_RETURNED_TOO_BIG,
        STATUS_RETURNED_TOO_SMALL,
        STATUS_RETURNED_OTHER,
    }
)

# Every valid status, used to validate updates.
VALID_STATUSES: frozenset[str] = TERMINAL_STATUSES | {STATUS_UNKNOWN}


# ---------------------------------------------------------------------------
# Identifier masking (Requirement 4.7)
# ---------------------------------------------------------------------------


def hash_order_number(order_number: str) -> str:
    """Return a one-way, stable hash of an ``order_number`` (Requirement 4.7).

    The raw order number is a direct customer identifier, so it is never stored on
    a :class:`RecommendationOutcome`. SHA-256 is one-way (the record cannot be used
    to recover the order number and re-identify the customer) and deterministic
    (the same order maps to the same hash), so a later return/exchange for the same
    order can still be matched back to its outcome via the hash.

    The input is stripped before hashing so trailing/leading whitespace in the raw
    data does not produce a different digest for the same logical order.
    """
    normalized = str(order_number).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# RecommendationOutcome record (design Data Models)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecommendationOutcome:
    """A learned outcome linking a recommendation to its result (design Data Models).

    Immutable: an update produces a new record (via :func:`dataclasses.replace`)
    rather than mutating in place, so a record handed to a reader cannot be changed
    underneath it. Direct customer identifiers are masked — only
    ``order_number_hash`` is retained, never the raw order number (Requirement 4.7).

    Fields:
    * ``outcome_id`` — opaque unique id for the record (used to update it later).
    * ``sku`` — the SKU the recommendation was for.
    * ``recommended_size`` — the size the agent recommended.
    * ``order_number_hash`` — one-way hash of the order number (masked identifier).
    * ``status`` — one of :data:`VALID_STATUSES`; starts at ``unknown``.
    * ``created_at`` / ``updated_at`` — UTC ISO-8601 timestamps.
    """

    outcome_id: str
    sku: str
    recommended_size: str
    order_number_hash: str
    status: str = STATUS_UNKNOWN
    created_at: str = ""
    updated_at: str = ""


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (timezone-aware)."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# In-memory outcome store
# ---------------------------------------------------------------------------


@dataclass
class OutcomeStore:
    """In-memory store of :class:`RecommendationOutcome` records.

    Holds every written outcome and maintains two indexes:

    * ``_by_id`` — ``outcome_id`` -> record, for fast update lookup.
    * ``_by_sku`` — ``sku`` -> list of ``outcome_id``, so the read tool
      ``get_recommendation_outcomes`` can aggregate a SKU's outcomes without
      scanning every record.

    The store is the write target behind the ``record_recommendation_outcome`` and
    outcome-update paths. It performs no I/O; a real deployment would back it with a
    datastore, but the interface (write new ``unknown``, update to a terminal
    status, read by SKU) stays the same so the reasoning layer is unaffected.

    Updates require **no human approval** and the updated record is immediately
    visible to subsequent reads for the same SKU (Requirement 4.5).
    """

    _by_id: Dict[str, RecommendationOutcome] = field(default_factory=dict)
    _by_sku: Dict[str, List[str]] = field(default_factory=dict)

    # -- write (Requirement 4.1) -------------------------------------------

    def add_outcome(
        self, sku: str, recommended_size: str, order_number: str
    ) -> RecommendationOutcome:
        """Write a new ``unknown`` outcome and return the stored record.

        The ``order_number`` is hashed on write (Requirement 4.7); the raw value is
        used only to compute the hash and is not retained. A fresh ``outcome_id`` is
        generated and both indexes are updated so the record is immediately
        readable by SKU and updatable by id.
        """
        now = _now_iso()
        outcome = RecommendationOutcome(
            outcome_id=uuid.uuid4().hex,
            sku=str(sku).strip(),
            recommended_size=str(recommended_size).strip(),
            order_number_hash=hash_order_number(order_number),
            status=STATUS_UNKNOWN,
            created_at=now,
            updated_at=now,
        )
        self._by_id[outcome.outcome_id] = outcome
        self._by_sku.setdefault(outcome.sku, []).append(outcome.outcome_id)
        return outcome

    # -- update (Requirements 4.2, 4.5) ------------------------------------

    def update_outcome(
        self, outcome_id: str, status: str
    ) -> RecommendationOutcome:
        """Update an outcome to a terminal status and return the updated record.

        ``status`` must be one of the four terminal statuses (``kept`` /
        ``returned too big`` / ``returned too small`` / ``returned other``);
        ``unknown`` is rejected because an update always moves a record forward.
        The update requires no human approval (Requirement 4.5) and refreshes
        ``updated_at``. Raises ``KeyError`` for an unknown ``outcome_id`` and
        ``ValueError`` for an invalid status.
        """
        if status not in TERMINAL_STATUSES:
            raise ValueError(
                f"invalid outcome status {status!r}; expected one of "
                f"{sorted(TERMINAL_STATUSES)}"
            )
        existing = self._by_id.get(outcome_id)
        if existing is None:
            raise KeyError(f"unknown outcome_id {outcome_id!r}")

        updated = replace(existing, status=status, updated_at=_now_iso())
        self._by_id[outcome_id] = updated
        return updated

    # -- reads --------------------------------------------------------------

    def get_outcome(self, outcome_id: str) -> Optional[RecommendationOutcome]:
        """Return the record for an ``outcome_id``, or ``None`` if absent."""
        return self._by_id.get(outcome_id)

    def get_outcomes_for_sku(self, sku: str) -> List[RecommendationOutcome]:
        """Return all outcomes recorded for a SKU (empty list if none).

        Backs the read tool ``get_recommendation_outcomes``: because updates land in
        ``_by_id`` by id, this always reflects the latest status for each record,
        making an updated outcome immediately available to the next recommendation
        for the same SKU without human approval (Requirement 4.5).
        """
        ids = self._by_sku.get(str(sku).strip(), [])
        return [self._by_id[outcome_id] for outcome_id in ids]


# ---------------------------------------------------------------------------
# Agent Tool #7 — record_recommendation_outcome (write path)
# ---------------------------------------------------------------------------


def record_recommendation_outcome(
    store: OutcomeStore,
    sku: str,
    recommended_size: str,
    order_number: str,
) -> Dict[str, str]:
    """Write an initial ``unknown`` recommendation outcome (Agent Tool #7).

    Links the recommendation to the ``sku``, the ``recommended_size``, and the
    order (via a one-way hash of ``order_number``), writing it to ``store`` with an
    initial status of ``unknown`` (Requirement 4.1). Direct customer identifiers are
    masked on write — the raw order number is hashed and never stored
    (Requirement 4.7).

    Returns the tool-contract shape ``{outcome_id, status: "unknown"}`` (design
    Agent Tool Catalog entry #7); the caller keeps the ``outcome_id`` to later
    update the record once the order's return/exchange result is known.
    """
    outcome = store.add_outcome(
        sku=sku, recommended_size=recommended_size, order_number=order_number
    )
    return {"outcome_id": outcome.outcome_id, "status": outcome.status}
