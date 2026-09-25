from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED

from fakes import ORDER_ID, SELLER_ID, TOOLS, FakeGateway, case, schema
from student_agent.agents import collect_evidence
from student_agent.agents.toolbox import CaseToolbox, RetryPolicy, ToolCallFailure
from student_agent.contracts import Contracts
from student_agent.permissions import TOOL_GRANTS, ToolPermissionError
from student_agent.state import Actor, CaseState, CrossCaseGuard, MissingReason
from student_agent.trace import TraceWriter

ROOT = Path(__file__).resolve().parents[1]
CASE_ID = "L3A_CASE_001"
SPECIALISTS = (Actor.ORDER_ITEM, Actor.PAYMENT, Actor.SHIPMENT)


async def _no_sleep(_delay: float) -> None:
    return None


@pytest.fixture
def contracts() -> Contracts:
    return Contracts(ROOT / "contracts" / "schemas")


@pytest.fixture
def trace(tmp_path: Path, contracts: Contracts) -> TraceWriter:
    return TraceWriter(tmp_path / "trace.jsonl", contracts)


def events_of(trace: TraceWriter) -> list[dict[str, Any]]:
    return [json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()]


def _collect(case_: dict[str, Any], gateway: FakeGateway, trace: TraceWriter) -> CaseState:
    return asyncio.run(
        collect_evidence(
            case_, gateway, trace, retry=RetryPolicy(sleep=_no_sleep), guard=CrossCaseGuard()
        )
    )


def test_every_call_carries_the_case_id_and_only_granted_tools(trace: TraceWriter) -> None:
    gateway = FakeGateway()
    state = _collect(case(), gateway, trace)

    assert gateway.calls, "specialists made no MCP call"
    assert {case_id for _, case_id, _ in gateway.calls} == {CASE_ID}
    specialist_tools = {
        "get_order",
        "get_order_items",
        "get_order_payments",
        "get_payment_timeline",
        "get_refund_timeline",
        "get_shipment_summary",
    }
    assert {tool for tool, _, _ in gateway.calls} == specialist_tools
    for tool in ("get_policy", "get_customer_history", "get_product_context"):
        assert gateway.called(tool) == 0
    for item in state.evidence:
        assert item.tool_name in TOOL_GRANTS[Actor(item.actor)]


def test_arguments_come_from_the_case_only(trace: TraceWriter) -> None:
    gateway = FakeGateway()
    _collect(case(), gateway, trace)
    assert all(args == {"order_id": ORDER_ID} for _, _, args in gateway.calls)


def test_order_item_tools_are_gated_by_case_topics(trace: TraceWriter) -> None:
    gateway = FakeGateway(issue="refund_pending")
    _collect(case("refund_pending"), gateway, trace)
    assert gateway.called("get_order") == 1
    assert gateway.called("get_order_items") == 0
    assert gateway.called("get_sellers") == 0


def test_foreign_order_evidence_is_rejected_before_consumption(trace: TraceWriter) -> None:
    def data(tool: str, _arguments: dict[str, str]) -> Any:
        if tool == "get_order":
            return {"order_id": "foreign-order", "order_status": "canceled"}
        return FakeGateway().data(tool, _arguments)

    gateway = FakeGateway(data=data)
    state = _collect(case("canceled_order_paid"), gateway, trace)
    assert not any(item.tool_name == "get_order" for item in state.evidence)
    assert any(
        entry.tool_name == "get_order" and entry.reason is MissingReason.DOMAIN_MISMATCH
        for entry in state.missing
    )


def test_scoped_gateway_rejects_tools_outside_the_actor_grant() -> None:
    gateway = FakeGateway()
    state = CaseState.start(case())
    toolbox = CaseToolbox(gateway, CASE_ID, TOOLS, state.ledger, CrossCaseGuard())
    payment = toolbox.scoped(Actor.PAYMENT)

    with pytest.raises(ToolPermissionError):
        asyncio.run(payment.call("get_order", order_id=ORDER_ID))
    with pytest.raises(ToolPermissionError):
        asyncio.run(toolbox.scoped(Actor.ORDER_ITEM).call("get_customer_history"))
    with pytest.raises(ValueError, match="case_id is fixed"):
        asyncio.run(payment.call("get_order_payments", case_id="L3A_CASE_999", order_id="x"))
    assert gateway.calls == []


def test_evidence_refs_are_exactly_what_the_gateway_issued(trace: TraceWriter) -> None:
    gateway = FakeGateway(issue="refund_failed")
    state = _collect(case("refund_failed"), gateway, trace)

    assert sorted(state.evidence_refs) == sorted(gateway.issued)
    consumed = [
        ref
        for event in events_of(trace)
        if event["event_type"] == "tool_result_consumed"
        for ref in event["evidence_refs"]
    ]
    assert sorted(consumed) == sorted(gateway.issued)
    state.ledger.require(state.evidence_refs)
    order = next(item for item in state.evidence if item.tool_name == "get_order")
    assert order.data["order_id"] == ORDER_ID
    assert order.result_hash.startswith("sha256:")


def test_missing_order_id_is_reported_not_guessed(trace: TraceWriter) -> None:
    gateway = FakeGateway()
    state = _collect(case(order_id=None), gateway, trace)

    assert gateway.calls == []
    reasons = {(entry.tool_name, entry.reason) for entry in state.missing}
    assert ("get_order", MissingReason.ID_NOT_IN_CASE) in reasons
    assert ("get_order_payments", MissingReason.ID_NOT_IN_CASE) in reasons
    assert state.evidence_refs == ()


def test_transient_errors_retry_twice_then_give_up(trace: TraceWriter) -> None:
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    gateway = FakeGateway(errors={"get_order_payments": [httpx2.ConnectError("down")] * 5})
    state = asyncio.run(
        collect_evidence(
            case(), gateway, trace, retry=RetryPolicy(sleep=record_sleep), guard=CrossCaseGuard()
        )
    )

    assert gateway.called("get_order_payments") == 3
    assert delays == [0.5, 1.0]
    assert any(
        entry.tool_name == "get_order_payments"
        and entry.reason is MissingReason.TRANSIENT_EXHAUSTED
        for entry in state.missing
    )


def test_transient_error_then_success_is_used(trace: TraceWriter) -> None:
    gateway = FakeGateway(
        errors={"get_shipment_summary": [MCPError(CONNECTION_CLOSED, "Connection closed")]}
    )
    state = _collect(case("unavailable_order_paid"), gateway, trace)

    assert gateway.called("get_shipment_summary") == 2
    assert "shipment" in state.evidence_by_domain
    assert not any(entry.tool_name == "get_shipment_summary" for entry in state.missing)


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        ("MCP tool get_order failed: order not found", MissingReason.NOT_FOUND),
        ("MCP tool get_order failed: 403 Forbidden for this case", MissingReason.FORBIDDEN),
        ("MCP tool get_order failed: Error executing tool get_order", MissingReason.TOOL_ERROR),
    ],
)
def test_server_errors_are_never_retried(
    trace: TraceWriter, message: str, reason: MissingReason
) -> None:
    gateway = FakeGateway(errors={"get_order": [RuntimeError(message)] * 5})
    state = _collect(case(), gateway, trace)

    assert gateway.called("get_order") == 1
    assert any(e.tool_name == "get_order" and e.reason is reason for e in state.missing)


def test_hex_ids_do_not_look_like_status_codes(trace: TraceWriter) -> None:
    gateway = FakeGateway(errors={"get_order": [RuntimeError("failed for a404b and c403d")]})
    state = _collect(case(), gateway, trace)
    assert any(e.reason is MissingReason.TOOL_ERROR for e in state.missing)


def test_same_tool_and_arguments_are_called_once(trace: TraceWriter) -> None:
    gateway = FakeGateway()
    _collect(case(), gateway, trace)
    keys = [(tool, tuple(sorted(args.items()))) for tool, _, args in gateway.calls]
    assert len(keys) == len(set(keys))


def test_follow_up_round_uses_ids_found_by_another_specialist(trace: TraceWriter) -> None:
    tools = {**TOOLS, "get_shipment_summary": schema("seller_id")}
    gateway = FakeGateway(tools=tools)
    state = _collect(case(), gateway, trace)

    assert ("get_shipment_summary", CASE_ID, {"seller_id": SELLER_ID}) in gateway.calls
    follow_ups = [
        event
        for event in events_of(trace)
        if event["event_type"] == "task_assigned" and event.get("decision_code") == "follow_up"
    ]
    assert [event["target"] for event in follow_ups] == [Actor.SHIPMENT.value]
    assert not any(entry.tool_name == "get_shipment_summary" for entry in state.missing)


def test_undiscovered_tool_and_foreign_domain_are_not_used(trace: TraceWriter) -> None:
    tools = {name: spec for name, spec in TOOLS.items() if name != "get_shipment_summary"}
    gateway = FakeGateway(tools=tools, domains={**FakeGateway().domains, "get_sellers": "payment"})
    state = _collect(case("unavailable_order_paid"), gateway, trace)

    assert gateway.called("get_shipment_summary") == 0
    reasons = {(entry.tool_name, entry.reason) for entry in state.missing}
    assert ("get_shipment_summary", MissingReason.TOOL_NOT_DISCOVERED) in reasons
    assert ("get_sellers", MissingReason.DOMAIN_MISMATCH) in reasons
    assert all(item.tool_name != "get_sellers" for item in state.evidence)


def test_collection_trace_order_and_schema(trace: TraceWriter, contracts: Contracts) -> None:
    _collect(case(), FakeGateway(), trace)
    events = events_of(trace)
    for number, event in enumerate(events, 1):
        contracts.validate_trace(event, f"event {number}")

    types = [event["event_type"] for event in events]
    assert "case_received" not in types and "case_finalized" not in types
    assert set(types) == {"task_assigned", "tool_result_consumed", "handoff"}
    for actor in (a.value for a in SPECIALISTS):
        own = [e["event_type"] for e in events if actor in (e["actor"], e.get("target"))]
        assert own[0] == "task_assigned"
        assert own[-1] == "handoff"
        assert own.count("handoff") == 1
        assert "tool_result_consumed" in own


def test_evidence_ref_cannot_serve_two_cases() -> None:
    guard = CrossCaseGuard()
    guard.claim("L3A_CASE_001", "ev_" + "a" * 32)
    with pytest.raises(ValueError, match="already belongs"):
        guard.claim("L3A_CASE_002", "ev_" + "a" * 32)


def test_toolbox_errors_carry_the_reason() -> None:
    failure = ToolCallFailure(MissingReason.NOT_FOUND, "get_order", "order not found")
    assert failure.reason is MissingReason.NOT_FOUND
    assert "get_order" in str(failure)
