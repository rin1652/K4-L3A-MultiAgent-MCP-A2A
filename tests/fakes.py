"""Offline stand-in for the MCP Evidence Gateway, shaped like real gateway payloads.

Test-only: refs are produced here because there is no network in unit tests. Nothing in
src/ may import this module.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from typing import Any

ORDER_ID = "e2a03ccf5ea816036608b2d8c3ab8e60"
SELLER_ID = "seller-e2a03ccf5ea8"
ITEM_ID = "item-e2a03ccf5ea8"


def schema(*required: str) -> dict[str, Any]:
    params = ("case_id", *required)
    return {
        "description": "fake",
        "input_schema": {
            "type": "object",
            "properties": {name: {"type": "string"} for name in params},
            "required": list(params),
        },
    }


TOOLS: dict[str, dict[str, Any]] = {
    "get_order": schema("order_id"),
    "get_order_items": schema("order_id"),
    "get_sellers": schema("order_id"),
    "get_order_payments": schema("order_id"),
    "get_payment_timeline": schema("order_id"),
    "get_refund_timeline": schema("order_id"),
    "get_shipment_summary": schema("order_id"),
    "get_product_context": schema("order_id"),
    "get_customer_history": schema("customer_unique_id"),
    "get_policy": schema("policy_version"),
}
DOMAINS = {
    "get_order": "order",
    "get_order_items": "item",
    "get_sellers": "seller",
    "get_order_payments": "payment",
    "get_payment_timeline": "payment",
    "get_refund_timeline": "refund",
    "get_shipment_summary": "shipment",
    "get_product_context": "product",
    "get_customer_history": "customer",
    "get_policy": "policy",
}

POLICY = {
    "currency": "BRL",
    "policy_version": "EC_POLICY_V1",
    "rules": {
        issue: {
            "case_status": status,
            "recommended_action": action,
            "refund_brl": 1.0,
            "responsible_parties": [
                {
                    "party_id": "seller-from-policy-example" if party == "seller" else None,
                    "party_type": party,
                }
            ],
        }
        for issue, (status, action, party) in {
            "canceled_order_paid": ("action_required", "issue_refund", "platform"),
            "unavailable_order_paid": ("action_required", "issue_refund", "seller"),
            "late_delivery_seller": ("action_required", "refund_freight", "seller"),
            "late_delivery_logistics": ("action_required", "refund_freight", "logistics_provider"),
            "valid_split_payment": ("no_action", "document_no_action", "customer"),
            "payment_mismatch": ("action_required", "reconcile_payment", "payment_provider"),
            "duplicate_charge": ("action_required", "refund_duplicate_charge", "payment_provider"),
            "refund_pending": ("needs_investigation", "monitor_refund", "payment_provider"),
            "refund_failed": ("action_required", "retry_refund", "payment_provider"),
            "unsupported_claim": ("no_action", "document_no_action", "customer"),
        }.items()
    },
}


def _t(day: str, hour: str = "09") -> str:
    return f"2018-{day}T{hour}:00:00-03:00"


def _pay(value: str, seq: str = "1", kind: str = "credit_card") -> dict[str, str]:
    return {
        "order_id": ORDER_ID,
        "payment_sequential": seq,
        "payment_type": kind,
        "payment_installments": "1",
        "payment_value": value,
    }


def _event(day: str, kind: str, amount: str, status: str = "confirmed") -> dict[str, str]:
    return {
        "order_id": ORDER_ID,
        "event_at": _t(day, "10"),
        "event_type": kind,
        "amount_brl": amount,
        "status": status,
    }


def _item(limit_day: str, price: str = "79.00", freight: str = "10.00") -> dict[str, str]:
    return {
        "order_id": ORDER_ID,
        "order_item_id": ITEM_ID,
        "product_id": "product-e2a03ccf5ea8",
        "seller_id": SELLER_ID,
        "shipping_limit_date": _t(limit_day),
        "price": price,
        "freight_value": freight,
    }


def world(issue: str) -> dict[str, Any]:
    """Case window is 2018-02-01 (purchase) .. 2018-02-12 (opened). Every scenario also
    carries decoy rows dated outside that window, like the real gateway does."""
    status, carrier, delivered, estimated = "delivered", "02-03", "02-08", "02-09"
    items = [_item("02-04"), _item("06-30", freight="18.00")]  # 2nd row is a decoy
    payments = [_pay("89.00"), _pay("16.00")]
    events = [_event("02-01", "captured", "89.00"), _event("06-01", "captured", "16.00")]
    refunds: list[dict[str, str]] | None = None
    ship_events = [
        {
            "event_at": _t("07-01"),
            "event_type": "delivered_late",
            "actor": "seller",
            "status": "confirmed",
            "order_id": ORDER_ID,
        }
    ]
    if issue == "canceled_order_paid":
        status, delivered = "canceled", None
        payments = [_pay("79.00"), _pay("18.00")]
        events = [_event("02-01", "captured", "79.00"), _event("06-01", "captured", "18.00")]
    elif issue == "unavailable_order_paid":
        status, delivered = "unavailable", None
        items = [_item("02-04", "89.00"), _item("02-04", "89.00")]  # identical duplicate rows
        payments = [_pay("89.00"), _pay("89.00")]
        events = [_event("02-01", "captured", "89.00"), _event("02-01", "captured", "89.00")]
    elif issue in ("late_delivery_seller", "late_delivery_logistics"):
        carrier = "02-06" if issue == "late_delivery_seller" else "02-04"
        delivered, estimated = "02-11", "02-09"
        items = [_item("02-05", freight="18.00"), _item("01-10")]
        paid = "18.00" if issue == "late_delivery_seller" else "16.00"
        payments = [_pay(paid), _pay("89.00")]
        events = [_event("02-01", "captured", paid), _event("01-05", "captured", "89.00")]
        actor = "seller" if issue == "late_delivery_seller" else "logistics_provider"
        ship_events = [
            {
                "event_at": _t("02-11"),
                "event_type": "delivered_late",
                "actor": actor,
                "status": "confirmed",
                "order_id": ORDER_ID,
            }
        ]
    elif issue == "valid_split_payment":
        payments = [_pay("44.50"), _pay("44.50", "2", "voucher"), _pay("52.00")]
        events = [
            _event("02-01", "captured", "44.50"),
            _event("02-01", "captured", "44.50"),
            _event("01-02", "captured", "52.00"),
        ]
        refunds = [_event("01-03", "refund_requested", "52.00", "failed")]
    elif issue == "payment_mismatch":
        payments = [_pay("35.00"), _pay("89.00")]
        events = [
            _event("02-01", "captured", "35.00"),
            _event("02-01", "reconciliation_mismatch", "35.00", "open"),
            _event("06-01", "captured", "89.00"),
        ]
        refunds = [_event("06-10", "refund_requested", "89.00", "pending")]
    elif issue == "duplicate_charge":
        payments = [_pay("64.00"), _pay("64.00", "2", "voucher")] * 2
        events = [
            _event("02-01", "captured", "64.00"),
            _event("02-01", "captured", "64.00"),
            _event("06-01", "captured", "64.00"),
            _event("06-01", "captured", "64.00"),
        ]
    elif issue == "refund_pending":
        events = [
            _event("02-01", "captured", "89.00"),
            _event("01-02", "captured", "35.00"),
            _event("01-02", "reconciliation_mismatch", "35.00", "open"),
        ]
        payments = [_pay("89.00"), _pay("35.00")]
        refunds = [_event("02-10", "refund_requested", "89.00", "pending")]
    elif issue == "refund_failed":
        payments = [_pay("52.00"), _pay("44.50"), _pay("44.50", "2", "voucher")]
        events = [
            _event("02-01", "captured", "52.00"),
            _event("01-02", "captured", "44.50"),
            _event("01-02", "captured", "44.50"),
        ]
        refunds = [_event("02-10", "refund_requested", "52.00", "failed")]
    order = {
        "order_id": ORDER_ID,
        "customer_id": "customer-row-e2a03ccf5ea8",
        "order_status": status,
        "order_purchase_timestamp": _t("02-01"),
        "order_approved_at": _t("02-01", "10"),
        "order_delivered_carrier_date": _t(carrier),
        "order_delivered_customer_date": _t(delivered) if delivered else None,
        "order_estimated_delivery_date": _t(estimated),
    }
    data: dict[str, Any] = {
        "get_order": order,
        "get_order_items": items,
        "get_sellers": [{"seller_id": SELLER_ID, "seller_city": "sao_paulo"}],
        "get_order_payments": payments,
        "get_payment_timeline": {"order_id": ORDER_ID, "payments": payments, "events": events},
        "get_shipment_summary": {
            "order_id": ORDER_ID,
            "order_status": status,
            "delivered_carrier_at": order["order_delivered_carrier_date"],
            "delivered_customer_at": order["order_delivered_customer_date"],
            "estimated_delivery_at": order["order_estimated_delivery_date"],
            "shipping_limits": [],
            "events": ship_events,
        },
        "get_policy": POLICY,
    }
    if refunds is not None:
        data["get_refund_timeline"] = {"order_id": ORDER_ID, "events": refunds}
    return data


def case(
    issue: str = "canceled_order_paid",
    order_id: str | None = ORDER_ID,
    case_id: str = "L3A_CASE_001",
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "language": "vi",
        "message": "test",
        "claims": [
            {"claim_id": "claim-a", "topic": issue},
            {"claim_id": "claim-b", "topic": "requested_full_refund"},
        ],
    }
    if order_id is not None:
        request["claimed_order_id"] = order_id
    return {
        "case_id": case_id,
        "opened_at": _t("02-12"),
        "customer_request": request,
        "policy_version": "EC_POLICY_V1",
    }


Responder = Callable[[str, dict[str, str]], Any]


class FakeGateway:
    """Records every call; returns schema-valid envelopes; errors can be scripted."""

    def __init__(
        self,
        issue: str = "canceled_order_paid",
        tools: dict[str, dict[str, Any]] | None = None,
        data: Responder | None = None,
        errors: dict[str, list[BaseException]] | None = None,
        domains: dict[str, str] | None = None,
    ) -> None:
        payloads = world(issue)
        self.tools = TOOLS if tools is None else tools
        self.data = data or (lambda tool, _args: copy.deepcopy(payloads.get(tool)))
        self.errors = errors or {}
        self.domains = domains or DOMAINS
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.issued: list[str] = []

    async def describe_tools(self) -> dict[str, dict[str, Any]]:
        return self.tools

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, dict(arguments)))
        queue = self.errors.get(tool_name)
        if queue:
            raise queue.pop(0)
        data = self.data(tool_name, arguments)
        if data is None:
            raise RuntimeError(f"MCP tool {tool_name} failed: Error executing tool {tool_name}")
        seed = f"{case_id}:{len(self.calls)}:{tool_name}"
        ref = f"ev_{hashlib.sha256(seed.encode()).hexdigest()[:32]}"
        self.issued.append(ref)
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": ref,
            "result_hash": "sha256:" + hashlib.sha256(json.dumps(data).encode()).hexdigest(),
            "domain": self.domains[tool_name],
            "data": data,
        }

    def called(self, tool: str) -> int:
        return [name for name, _, _ in self.calls].count(tool)
