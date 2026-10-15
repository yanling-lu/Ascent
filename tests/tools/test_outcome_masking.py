"""Unit tests for outcome identifier masking (Task 8.2).

Covers the write/update side of the learning loop in ``ai_fit.tools.outcomes``,
focused on the privacy guarantee that direct customer identifiers are masked:

* Property 14 (outcome-record portion): emitted artifacts always mask customer
  identifiers — the raw ``order_number`` never appears in a stored
  :class:`RecommendationOutcome`; only ``order_number_hash`` is retained.
* The hash is a stable, one-way SHA-256 digest (same order -> same hash,
  different order -> different hash).
* The write path returns the tool contract ``{outcome_id, status: "unknown"}``.
* The update path moves ``unknown`` to each terminal status, requires no human
  approval, and ``get_outcomes_for_sku`` immediately reflects the update.

Validates: Requirements 4.1, 4.2, 4.5, 4.7
(Property 14 — outcome-record portion — also covers 7.5, 10.3.)
"""

from __future__ import annotations

import dataclasses
import hashlib

import pytest

from ai_fit.tools.outcomes import (
    STATUS_KEPT,
    STATUS_RETURNED_OTHER,
    STATUS_RETURNED_TOO_BIG,
    STATUS_RETURNED_TOO_SMALL,
    STATUS_UNKNOWN,
    TERMINAL_STATUSES,
    OutcomeStore,
    RecommendationOutcome,
    hash_order_number,
    record_recommendation_outcome,
)

# A raw order number that is a direct customer identifier and must never be
# stored verbatim on a RecommendationOutcome.
RAW_ORDER = "CA-ORDER-7788991"


# ---------------------------------------------------------------------------
# hash_order_number: stable one-way masking (Requirement 4.7)
# ---------------------------------------------------------------------------


def test_hash_is_sha256_hex_digest():
    """The hash is a SHA-256 hex digest of the stripped order number."""
    expected = hashlib.sha256(RAW_ORDER.encode("utf-8")).hexdigest()
    assert hash_order_number(RAW_ORDER) == expected
    # SHA-256 hex digests are 64 lowercase hex chars.
    assert len(hash_order_number(RAW_ORDER)) == 64


def test_hash_is_stable_same_order_same_hash():
    """Same order number -> same hash, so a later return can be matched back."""
    assert hash_order_number(RAW_ORDER) == hash_order_number(RAW_ORDER)
    # Whitespace is stripped so the raw-data variant hashes identically.
    assert hash_order_number(f"  {RAW_ORDER}  ") == hash_order_number(RAW_ORDER)


def test_hash_differs_for_different_orders():
    """Different order numbers -> different hashes."""
    assert hash_order_number(RAW_ORDER) != hash_order_number("CA-ORDER-0000001")


def test_hash_does_not_contain_raw_order_number():
    """The one-way hash does not leak the raw identifier."""
    digest = hash_order_number(RAW_ORDER)
    assert RAW_ORDER not in digest


# ---------------------------------------------------------------------------
# Write path: masks identifier, returns {outcome_id, status: "unknown"}
# (Requirements 4.1, 4.7)
# ---------------------------------------------------------------------------


def test_record_outcome_returns_unknown_contract_shape():
    """record_recommendation_outcome returns {outcome_id, status: 'unknown'}."""
    store = OutcomeStore()
    result = record_recommendation_outcome(
        store, sku="SKU-123", recommended_size="8.5", order_number=RAW_ORDER
    )
    assert set(result) == {"outcome_id", "status"}
    assert result["status"] == STATUS_UNKNOWN
    assert isinstance(result["outcome_id"], str) and result["outcome_id"]


def test_stored_outcome_masks_raw_order_number():
    """The stored record retains only the hash — never the raw order number."""
    store = OutcomeStore()
    result = record_recommendation_outcome(
        store, sku="SKU-123", recommended_size="8.5", order_number=RAW_ORDER
    )
    stored = store.get_outcome(result["outcome_id"])
    assert stored is not None

    # Only the hash is kept, equal to the stable digest of the raw order.
    assert stored.order_number_hash == hash_order_number(RAW_ORDER)

    # The raw order number appears in no field of the stored record.
    for value in dataclasses.asdict(stored).values():
        assert RAW_ORDER not in str(value)

    # Belt-and-suspenders: the record has no attribute holding the raw value.
    assert not hasattr(stored, "order_number")


def test_add_outcome_links_sku_and_size_and_starts_unknown():
    """The written record links SKU + recommended size and starts 'unknown'."""
    store = OutcomeStore()
    outcome = store.add_outcome(
        sku="SKU-999", recommended_size="9", order_number=RAW_ORDER
    )
    assert isinstance(outcome, RecommendationOutcome)
    assert outcome.sku == "SKU-999"
    assert outcome.recommended_size == "9"
    assert outcome.status == STATUS_UNKNOWN
    assert outcome.order_number_hash == hash_order_number(RAW_ORDER)


# ---------------------------------------------------------------------------
# Update path: unknown -> each terminal status (Requirements 4.2, 4.5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "terminal_status",
    [
        STATUS_KEPT,
        STATUS_RETURNED_TOO_BIG,
        STATUS_RETURNED_TOO_SMALL,
        STATUS_RETURNED_OTHER,
    ],
)
def test_update_moves_unknown_to_each_terminal_status(terminal_status):
    """An outcome updates from 'unknown' to each terminal status, no approval."""
    store = OutcomeStore()
    outcome = store.add_outcome(
        sku="SKU-123", recommended_size="8.5", order_number=RAW_ORDER
    )
    assert outcome.status == STATUS_UNKNOWN

    updated = store.update_outcome(outcome.outcome_id, terminal_status)
    assert updated.outcome_id == outcome.outcome_id
    assert updated.status == terminal_status
    # The masked identifier is preserved across the update.
    assert updated.order_number_hash == outcome.order_number_hash


def test_update_rejects_non_terminal_status():
    """Updating to 'unknown' (or any invalid status) is rejected."""
    store = OutcomeStore()
    outcome = store.add_outcome(
        sku="SKU-123", recommended_size="8.5", order_number=RAW_ORDER
    )
    with pytest.raises(ValueError):
        store.update_outcome(outcome.outcome_id, STATUS_UNKNOWN)
    with pytest.raises(ValueError):
        store.update_outcome(outcome.outcome_id, "not-a-status")


def test_update_rejects_unknown_outcome_id():
    """Updating a non-existent outcome id raises KeyError."""
    store = OutcomeStore()
    with pytest.raises(KeyError):
        store.update_outcome("does-not-exist", STATUS_KEPT)


# ---------------------------------------------------------------------------
# get_outcomes_for_sku reflects updates (Requirement 4.5)
# ---------------------------------------------------------------------------


def test_get_outcomes_for_sku_reflects_update():
    """A SKU read immediately reflects the updated terminal status."""
    store = OutcomeStore()
    outcome = store.add_outcome(
        sku="SKU-ABC", recommended_size="7", order_number=RAW_ORDER
    )

    before = store.get_outcomes_for_sku("SKU-ABC")
    assert [o.status for o in before] == [STATUS_UNKNOWN]

    store.update_outcome(outcome.outcome_id, STATUS_RETURNED_TOO_BIG)

    after = store.get_outcomes_for_sku("SKU-ABC")
    assert [o.status for o in after] == [STATUS_RETURNED_TOO_BIG]
    assert after[0].outcome_id == outcome.outcome_id


def test_get_outcomes_for_sku_aggregates_multiple_and_masks_all():
    """Multiple outcomes for one SKU are returned, each masking its identifier."""
    store = OutcomeStore()
    store.add_outcome(sku="SKU-X", recommended_size="8", order_number="ORDER-A")
    store.add_outcome(sku="SKU-X", recommended_size="8.5", order_number="ORDER-B")

    outcomes = store.get_outcomes_for_sku("SKU-X")
    assert len(outcomes) == 2
    hashes = {o.order_number_hash for o in outcomes}
    assert hashes == {hash_order_number("ORDER-A"), hash_order_number("ORDER-B")}
    # No raw order number is present on any aggregated record.
    for o in outcomes:
        for value in dataclasses.asdict(o).values():
            assert "ORDER-A" not in str(value) and "ORDER-B" not in str(value)


def test_get_outcomes_for_absent_sku_is_empty():
    """A SKU with no recorded outcomes returns an empty list."""
    assert OutcomeStore().get_outcomes_for_sku("NOPE") == []


def test_terminal_statuses_exclude_unknown():
    """The terminal set is exactly the four resolved statuses, no 'unknown'."""
    assert TERMINAL_STATUSES == frozenset(
        {
            STATUS_KEPT,
            STATUS_RETURNED_TOO_BIG,
            STATUS_RETURNED_TOO_SMALL,
            STATUS_RETURNED_OTHER,
        }
    )
    assert STATUS_UNKNOWN not in TERMINAL_STATUSES
