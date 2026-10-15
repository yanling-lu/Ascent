"""Human-review routing tool and audit log (Task 8.7).

This module implements Agent Tool #9 from the design's tool catalog,
``route_to_human_reviewer``, together with the in-memory audit log it writes to.

The AI_Fit_Agent has **no financial authority**: it may present size
recommendations, occasion assessments, and alternative-style suggestions, but it
must never execute an action that modifies order state, pricing, or payment
(Requirement 11.1). When a request would involve a refund, a price change, a
discount, or an order modification — or when a customer asks for one of those
directly — the agent routes the action to a Human_Reviewer instead of acting on
it (Requirements 11.2, 11.3). Routing is **intent-recording only**: this tool
appends an audit entry and returns a routing confirmation; it never touches any
order, pricing, or payment state.

Each routed action creates an audit log entry recording the triggering request,
the routed action type, and a timestamp (Requirement 11.6), matching the
``AuditLogEntry`` data model (``audit_id``, ``triggering_request``,
``routed_action_type``, ``timestamp``).

Design references:
* Agent Tool Catalog entry #9 ``route_to_human_reviewer``
  (``{triggering_request, action_type}`` -> ``{routed: true, audit_id, timestamp}``).
* ``AuditLogEntry`` data model.
* Requirements 11.1, 11.2, 11.3, 11.6.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional
from uuid import uuid4


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditLogEntry:
    """A single human-review audit record (Requirement 11.6).

    Mirrors the design's ``AuditLogEntry`` data model. Instances are immutable:
    once an action has been routed, its audit record is a fixed historical fact.

    Attributes:
        audit_id: Unique identifier for this audit entry.
        triggering_request: The request that triggered the routing (verbatim, as
            supplied by the orchestrator — e.g. the customer's refund request).
        routed_action_type: The type of action routed to the Human_Reviewer
            (e.g. ``"refund"``, ``"price_change"``, ``"discount"``,
            ``"order_modification"``).
        timestamp: ISO-8601 UTC timestamp of when the action was routed.
    """

    audit_id: str
    triggering_request: str
    routed_action_type: str
    timestamp: str


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


class AuditLog:
    """In-memory append-only store of :class:`AuditLogEntry` records.

    The log records the intent to route a financial/order action to a
    Human_Reviewer. It deliberately holds no order, pricing, or payment state and
    exposes no way to mutate it (Requirement 11.1): entries can only be appended
    and read back.

    For the hackathon prototype the log lives in memory; the ``append`` surface is
    the single seam a durable audit store would replace without touching the
    routing tool.
    """

    def __init__(self) -> None:
        self._entries: List[AuditLogEntry] = []

    def append(self, entry: AuditLogEntry) -> None:
        """Append a completed audit entry to the log."""
        self._entries.append(entry)

    @property
    def entries(self) -> List[AuditLogEntry]:
        """Return a shallow copy of the recorded entries in append order.

        A copy is returned so callers cannot mutate the log's internal list; the
        entries themselves are frozen dataclasses and so are safe to share.
        """
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)


# ---------------------------------------------------------------------------
# Tool #9 — route_to_human_reviewer
# ---------------------------------------------------------------------------


def route_to_human_reviewer(
    triggering_request: str,
    action_type: str,
    audit_log: AuditLog,
    *,
    _now: Optional[Callable[[], datetime]] = None,
) -> Dict[str, object]:
    """Route a financial/order action to a Human_Reviewer, recording intent only.

    This is Agent Tool #9. It records the *intent* to have a human handle a
    refund, price change, discount, or order modification; it never modifies any
    order, pricing, or payment state (Requirement 11.1). The routed action is
    appended to ``audit_log`` as an :class:`AuditLogEntry` capturing the
    triggering request, the routed action type, and a timestamp (Requirement
    11.6).

    Args:
        triggering_request: The request that triggered routing (e.g. the
            customer's refund/discount/order-modification request).
        action_type: The type of action being routed to the Human_Reviewer.
        audit_log: The :class:`AuditLog` to append the entry to.
        _now: Test seam for the clock; defaults to :func:`datetime.now` in UTC.

    Returns:
        A dict matching the design's tool-catalog output shape:
        ``{"routed": True, "audit_id": <str>, "timestamp": <ISO-8601 str>}``.
    """
    clock = _now if _now is not None else (lambda: datetime.now(timezone.utc))
    timestamp = clock().isoformat()
    audit_id = str(uuid4())

    entry = AuditLogEntry(
        audit_id=audit_id,
        triggering_request=triggering_request,
        routed_action_type=action_type,
        timestamp=timestamp,
    )
    audit_log.append(entry)

    return {"routed": True, "audit_id": audit_id, "timestamp": timestamp}
