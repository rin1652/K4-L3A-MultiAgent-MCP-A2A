"""Deterministic facts extracted from one case's evidence (no guessing, no LLM).

Gateway rows for an order can include rows from other timelines. Only rows whose
timestamp lies inside the case window ``[order_purchase_timestamp, opened_at]`` count as
the case's own history; everything else is reported as excluded, never silently used.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..state import CaseState, EvidenceItem

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
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


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

    order, facts.refs["order"] = _data(evidence, "get_order")
    if isinstance(order, Mapping):
        facts.order_id = order.get("order_id")
        facts.order_status = order.get("order_status")
        facts.purchase_at = parse_time(order.get("order_purchase_timestamp"))
        facts.carrier_at = parse_time(order.get("order_delivered_carrier_date"))
        facts.delivered_at = parse_time(order.get("order_delivered_customer_date"))
        facts.estimated_at = parse_time(order.get("order_estimated_delivery_date"))

    items, facts.refs["items"] = _data(evidence, "get_order_items")
    for row in _dedupe(_rows(items)):
        if facts.in_window(parse_time(row.get("shipping_limit_date"))):
            facts.items.append(row)
        else:
            facts.excluded["item_outside_window"] += 1
    facts.seller_ids = list(
        dict.fromkeys(str(row["seller_id"]) for row in facts.items if row.get("seller_id"))
    )
    _, facts.refs["sellers"] = _data(evidence, "get_sellers")

    payments, facts.refs["payments"] = _data(evidence, "get_order_payments")
    facts.payment_rows = _rows(payments)
    timeline, facts.refs["payment_timeline"] = _data(evidence, "get_payment_timeline")
    if isinstance(timeline, Mapping):
        if not facts.payment_rows:
            facts.payment_rows = _rows(timeline.get("payments"))
        events = _money_events(facts, timeline.get("events"), "payment_event")
        facts.captures = [e for e in events if e.event_type == "captured"]
        facts.mismatches = [
            e for e in events if e.event_type == "reconciliation_mismatch" and e.status == "open"
        ]

    refunds, facts.refs["refund_timeline"] = _data(evidence, "get_refund_timeline")
    if isinstance(refunds, Mapping):
        facts.refunds = _money_events(facts, refunds.get("events"), "refund_event")

    shipment, facts.refs["shipment"] = _data(evidence, "get_shipment_summary")
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

    _, facts.refs["policy"] = _data(evidence, "get_policy")
    facts.refs = {name: ref for name, ref in facts.refs.items() if ref}
    return facts
