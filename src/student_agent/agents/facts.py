"""Deterministic facts extracted from one case's evidence (no guessing, no LLM).

Gateway rows for an order can include rows from other timelines. Only rows whose
timestamp lies inside the case window ``[order_purchase_timestamp, opened_at]`` count as
the case's own history; everything else is reported as excluded, never silently used.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..state import CaseState, DataConflict, EvidenceItem, iter_id_fields

CENT = 0.005


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def parse_amount(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(amount) or amount < 0:
        return None
    return round(amount, 2)


@dataclass(frozen=True)
class MoneyEvent:
    at: datetime
    event_type: str
    amount: float
    status: str


@dataclass
class CaseFacts:
    order_id: str | None = None
    order_status: str | None = None
    purchase_at: datetime | None = None
    opened_at: datetime | None = None
    carrier_at: datetime | None = None
    delivered_at: datetime | None = None
    estimated_at: datetime | None = None
    items: list[Mapping[str, Any]] = field(default_factory=list)
    seller_ids: list[str] = field(default_factory=list)
    payment_rows: list[Mapping[str, Any]] = field(default_factory=list)
    captures: list[MoneyEvent] = field(default_factory=list)
    mismatches: list[MoneyEvent] = field(default_factory=list)
    refunds: list[MoneyEvent] = field(default_factory=list)
    late_event_actors: list[str] = field(default_factory=list)
    excluded: Counter[str] = field(default_factory=Counter)
    refs: dict[str, str] = field(default_factory=dict)
    conflicts: list[DataConflict] = field(default_factory=list)
    policy_valid: bool = False

    # --- derived -------------------------------------------------------------------
    def in_window(self, at: datetime | None) -> bool:
        if at is None or self.opened_at is None:
            return False
        lower_ok = self.purchase_at is None or at >= self.purchase_at
        return lower_ok and at <= self.opened_at

    @property
    def order_total(self) -> float | None:
        totals = [
            (parse_amount(item.get("price")), parse_amount(item.get("freight_value")))
            for item in self.items
        ]
        if not totals or any(p is None or f is None for p, f in totals):
            return None
        return round(sum(p + f for p, f in totals), 2)  # type: ignore[operator]

    @property
    def item_freight(self) -> float | None:
        values = [parse_amount(item.get("freight_value")) for item in self.items]
        if not values or any(value is None for value in values):
            return None
        return round(sum(values), 2)  # type: ignore[arg-type]

    @property
    def shipping_limit(self) -> datetime | None:
        limits = [parse_time(item.get("shipping_limit_date")) for item in self.items]
        known = [limit for limit in limits if limit is not None]
        return max(known) if known else None

    def captured_by_amount(self) -> dict[float, int]:
        """Own capture count per amount, capped by distinct payment rows of that amount.

        Identical duplicated rows (same sequential/type/installments/value) count once;
        two different instruments with the same amount count twice.
        """
        events = Counter(event.amount for event in self.captures)
        distinct_rows: Counter[float] = Counter()
        seen: set[str] = set()
        for row in self.payment_rows:
            key = json.dumps(row, sort_keys=True)
            amount = parse_amount(row.get("payment_value"))
            if amount is None or key in seen:
                continue
            seen.add(key)
            distinct_rows[amount] += 1
        if not distinct_rows:
            return dict(events)
        return {amount: min(count, distinct_rows[amount]) for amount, count in events.items()}

    @property
    def captured_total(self) -> float:
        return round(sum(a * n for a, n in self.captured_by_amount().items()), 2)

    @property
    def refunded_total(self) -> float:
        completed = {"completed", "complete", "succeeded", "success", "refunded", "processed"}
        return round(
            sum(event.amount for event in self.refunds if event.status.lower() in completed), 2
        )

    @property
    def is_late(self) -> bool | None:
        if self.delivered_at is None or self.estimated_at is None:
            return None
        return self.delivered_at > self.estimated_at

    @property
    def seller_missed_limit(self) -> bool | None:
        limit = self.shipping_limit
        if self.carrier_at is None or limit is None:
            return None
        return self.carrier_at > limit


def _data(evidence: Iterable[EvidenceItem], tool: str) -> tuple[Any, str | None]:
    for item in evidence:
        if item.tool_name == tool:
            return item.data, item.evidence_ref
    return None, None


def _order_ids(value: Any) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(identifier for key, identifier in iter_id_fields(value) if key == "order_id")
    )


def _scoped_value(
    value: Any,
    expected_order_id: str | None,
    facts: CaseFacts,
    tool_name: str,
) -> Any:
    """Keep only rows tied to the requested order; unrelated rows are not evidence."""
    if expected_order_id is None:
        return value
    if isinstance(value, Mapping):
        ids = _order_ids(value)
        if ids and any(identifier != expected_order_id for identifier in ids):
            facts.conflicts.append(
                DataConflict(
                    field="order_id",
                    sources=("requested_order_id", tool_name),
                    selected_source=None,
                    resolution_code="order_identity_mismatch",
                    observed={tool_name: ",".join(ids)},
                )
            )
            return None
        return value
    if isinstance(value, list):
        rows: list[Any] = []
        for row in value:
            ids = _order_ids(row)
            if ids and any(identifier != expected_order_id for identifier in ids):
                facts.conflicts.append(
                    DataConflict(
                        field="order_id",
                        sources=("requested_order_id", tool_name),
                        selected_source=None,
                        resolution_code="order_identity_mismatch",
                        observed={tool_name: ",".join(ids)},
                    )
                )
                continue
            rows.append(row)
        return rows
    return value


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, Mapping)]
    return []


def _dedupe(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    unique: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        unique.setdefault(json.dumps(row, sort_keys=True), row)
    return list(unique.values())


def _money_events(facts: CaseFacts, events: Any, label: str) -> list[MoneyEvent]:
    own = []
    for row in _rows(events):
        at = parse_time(row.get("event_at"))
        amount = parse_amount(row.get("amount_brl"))
        if at is None or amount is None:
            facts.excluded[f"{label}_unparsed"] += 1
            continue
        if not facts.in_window(at):
            facts.excluded[f"{label}_outside_window"] += 1
            continue
        own.append(
            MoneyEvent(at, str(row.get("event_type", "")), amount, str(row.get("status", "")))
        )
    return sorted(own, key=lambda event: event.at)


def extract_facts(state: CaseState) -> CaseFacts:
    facts = CaseFacts(opened_at=parse_time(state.case.get("opened_at")))
    evidence = state.evidence
    request = state.case.get("customer_request")
    claimed_order_id = request.get("claimed_order_id") if isinstance(request, Mapping) else None
    expected_order_id = claimed_order_id if isinstance(claimed_order_id, str) else None

    order, order_ref = _data(evidence, "get_order")
    order = _scoped_value(order, expected_order_id, facts, "get_order")
    if isinstance(order, Mapping):
        facts.order_id = order.get("order_id")
        if isinstance(facts.order_id, str):
            expected_order_id = facts.order_id
        facts.order_status = order.get("order_status")
        facts.purchase_at = parse_time(order.get("order_purchase_timestamp"))
        facts.carrier_at = parse_time(order.get("order_delivered_carrier_date"))
        facts.delivered_at = parse_time(order.get("order_delivered_customer_date"))
        facts.estimated_at = parse_time(order.get("order_estimated_delivery_date"))
        if order_ref:
            facts.refs["order"] = order_ref
    elif expected_order_id is not None and order_ref:
        facts.conflicts.append(
            DataConflict(
                field="order_id",
                sources=("customer_request.claimed_order_id", "get_order"),
                selected_source=None,
                resolution_code="order_identity_mismatch",
                observed={"customer_request.claimed_order_id": expected_order_id},
            )
        )

    items, items_ref = _data(evidence, "get_order_items")
    items = _scoped_value(items, expected_order_id, facts, "get_order_items")
    for row in _dedupe(_rows(items)):
        if facts.in_window(parse_time(row.get("shipping_limit_date"))):
            facts.items.append(row)
        else:
            facts.excluded["item_outside_window"] += 1
    if facts.items and items_ref:
        facts.refs["items"] = items_ref
    facts.seller_ids = list(
        dict.fromkeys(str(row["seller_id"]) for row in facts.items if row.get("seller_id"))
    )
    sellers, sellers_ref = _data(evidence, "get_sellers")
    sellers = _scoped_value(sellers, expected_order_id, facts, "get_sellers")
    if isinstance(sellers, list) and sellers_ref and sellers:
        facts.refs["sellers"] = sellers_ref

    payments, payments_ref = _data(evidence, "get_order_payments")
    payments = _scoped_value(payments, expected_order_id, facts, "get_order_payments")
    facts.payment_rows = _rows(payments)
    if facts.payment_rows and payments_ref:
        facts.refs["payments"] = payments_ref
    timeline, timeline_ref = _data(evidence, "get_payment_timeline")
    timeline = _scoped_value(timeline, expected_order_id, facts, "get_payment_timeline")
    if isinstance(timeline, Mapping):
        if not facts.payment_rows:
            facts.payment_rows = _rows(timeline.get("payments"))
        events = _money_events(facts, timeline.get("events"), "payment_event")
        facts.captures = [e for e in events if e.event_type == "captured"]
        facts.mismatches = [
            e for e in events if e.event_type == "reconciliation_mismatch" and e.status == "open"
        ]
        if timeline_ref:
            facts.refs["payment_timeline"] = timeline_ref

    refunds, refund_ref = _data(evidence, "get_refund_timeline")
    refunds = _scoped_value(refunds, expected_order_id, facts, "get_refund_timeline")
    if isinstance(refunds, Mapping):
        facts.refunds = _money_events(facts, refunds.get("events"), "refund_event")
        if facts.refunds and refund_ref:
            facts.refs["refund_timeline"] = refund_ref

    shipment, shipment_ref = _data(evidence, "get_shipment_summary")
    shipment = _scoped_value(shipment, expected_order_id, facts, "get_shipment_summary")
    if isinstance(shipment, Mapping):
        if facts.delivered_at is None:
            facts.delivered_at = parse_time(shipment.get("delivered_customer_at"))
        if facts.estimated_at is None:
            facts.estimated_at = parse_time(shipment.get("estimated_delivery_at"))
        if facts.carrier_at is None:
            facts.carrier_at = parse_time(shipment.get("delivered_carrier_at"))
        for event in _rows(shipment.get("events")):
            at = parse_time(event.get("event_at"))
            # A delivery event only belongs to this case if it matches the delivery row.
            if (
                event.get("event_type") == "delivered_late"
                and at is not None
                and facts.delivered_at is not None
                and at == facts.delivered_at
            ):
                facts.late_event_actors.append(str(event.get("actor", "")))
            else:
                facts.excluded["shipment_event_unmatched"] += 1
        if shipment_ref:
            facts.refs["shipment"] = shipment_ref

    policy, policy_ref = _data(evidence, "get_policy")
    expected_policy_version = state.case.get("policy_version")
    if (
        isinstance(policy, Mapping)
        and isinstance(expected_policy_version, str)
        and policy.get("policy_version") == expected_policy_version
        and policy.get("currency") == "BRL"
        and isinstance(policy.get("rules"), Mapping)
    ):
        facts.policy_valid = True
        if policy_ref:
            facts.refs["policy"] = policy_ref
    elif policy_ref:
        facts.conflicts.append(
            DataConflict(
                field="policy",
                sources=("case.policy_version", "get_policy"),
                selected_source=None,
                resolution_code="policy_unverified",
                observed={
                    "case.policy_version": str(expected_policy_version),
                    "get_policy.policy_version": str(
                        policy.get("policy_version") if isinstance(policy, Mapping) else None
                    ),
                },
            )
        )
    return facts
