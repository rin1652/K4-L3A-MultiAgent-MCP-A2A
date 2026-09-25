from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED

from student_agent.agents import collect_evidence
from student_agent.agents.toolbox import CaseToolbox, RetryPolicy, ToolCallFailure
from student_agent.contracts import Contracts
from student_agent.permissions import TOOL_GRANTS, ToolPermissionError
from student_agent.state import Actor, CaseState, CrossCaseGuard, MissingReason
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

ROOT = Path(__file__).resolve().parents[1]
CASE_ID = "L3A_CASE_001"
ORDER_ID = "e2a03ccf5ea816036608b2d8c3ab8e60"


def _schema(*required: str) -> dict[str, Any]:
    params = ("case_id", *required)
    return {
        "input_schema": {
            "type": "object",
            "properties": {name: {"type": "string"} for name in params},
            "required": list(params),
        },
        "description": "fake",
    }


DEFAULT_TOOLS = {
    "get_order": _schema("order_id"),
    "get_seller": _schema("seller_id"),
    "get_payment": _schema("order_id"),
    "get_shipment": _schema("order_id"),
    "get_policy": _schema("policy_version"),
}
DOMAINS = {
    "get_order": "order",
    "get_seller": "seller",
    "get_payment": "payment",
    "get_shipment": "shipment",
    "get_policy": "policy",
}

Responder = Callable[[str, dict[str, str]], Any]


def _default_data(tool: str, args: dict[str, str]) -> Any:
    if tool == "get_order":
        return {
            "order_id": args["order_id"],
            "order_status": "canceled",
            "items": [{"order_item_id": 1, "seller_id": "seller-aaa"}],
        }
    return {"echo": args}


class FakeGateway:
    """Mimics EvidenceGateway: records every call, returns schema-valid envelopes."""

    def __init__(
        self,
        tools: dict[str, dict[str, Any]] | None = None,
        data: Responder = _default_data,
        errors: dict[str, list[BaseException]] | None = None,
        domains: dict[str, str] | None = None,
    ) -> None:
        self.tools = DEFAULT_TOOLS if tools is None else tools
        self.data = data
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
        ref = f"ev_{hashlib.sha256(f'{len(self.calls)}{tool_name}'.encode()).hexdigest()[:32]}"
        self.issued.append(ref)
        data = self.data(tool_name, arguments)
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": ref,
            "result_hash": "sha256:" + hashlib.sha256(json.dumps(data).encode()).hexdigest(),
            "domain": self.domains[tool_name],
            "data": data,
        }


def _case(order_id: str | None = ORDER_ID, case_id: str = CASE_ID) -> dict[str, Any]:
    request: dict[str, Any] = {
        "language": "vi",
        "message": "test",
        "claims": [{"claim_id": "claim-001-a", "topic": "canceled_order_paid"}],
    }
    if order_id is not None:
        request["claimed_order_id"] = order_id
    return {"case_id": case_id, "customer_request": request, "policy_version": "EC_POLICY_V1"}


async def _no_sleep(_delay: float) -> None:
    return None


@pytest.fixture
def contracts() -> Contracts:
    return Contracts(ROOT / "contracts" / "schemas")


@pytest.fixture
def trace(tmp_path: Path, contracts: Contracts) -> TraceWriter:
    return TraceWriter(tmp_path / "trace.jsonl", contracts)


def _events(trace: TraceWriter) -> list[dict[str, Any]]:
    return [json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()]


def _collect(case: dict[str, Any], gateway: FakeGateway, trace: TraceWriter) -> CaseState:
    return asyncio.run(
        collect_evidence(
            case,
            gateway,
            trace,
            retry=RetryPolicy(sleep=_no_sleep),
            guard=CrossCaseGuard(),
        )
    )


def test_every_call_carries_the_case_id_and_only_granted_tools(trace: TraceWriter) -> None:
    gateway = FakeGateway()
    state = _collect(_case(), gateway, trace)

    assert gateway.calls, "specialists made no MCP call"
    assert {case_id for _, case_id, _ in gateway.calls} == {CASE_ID}
    specialist_tools = {
        tool
        for actor in (Actor.ORDER_ITEM, Actor.PAYMENT, Actor.SHIPMENT)
        for tool in TOOL_GRANTS[actor]
    }
    assert {tool for tool, _, _ in gateway.calls} <= specialist_tools
    assert "get_policy" not in {tool for tool, _, _ in gateway.calls}
    for item in state.evidence:
        assert item.tool_name in TOOL_GRANTS[Actor(item.actor)]


def test_order_id_comes_from_case_and_seller_id_from_order_evidence(trace: TraceWriter) -> None:
    gateway = FakeGateway()
    _collect(_case(), gateway, trace)

    calls = {tool: args for tool, _, args in gateway.calls}
    assert calls["get_order"] == {"order_id": ORDER_ID}
    assert calls["get_payment"] == {"order_id": ORDER_ID}
    assert calls["get_seller"] == {"seller_id": "seller-aaa"}


def test_scoped_gateway_rejects_tools_outside_the_actor_grant(trace: TraceWriter) -> None:
    gateway = FakeGateway()
    state = CaseState.start(_case())
    toolbox = CaseToolbox(gateway, CASE_ID, DEFAULT_TOOLS, state.ledger, CrossCaseGuard())
    payment = toolbox.scoped(Actor.PAYMENT)

    with pytest.raises(ToolPermissionError):
        asyncio.run(payment.call("get_order", order_id=ORDER_ID))
    with pytest.raises(ValueError, match="case_id is fixed"):
        asyncio.run(payment.call("get_payment", case_id="L3A_CASE_999", order_id=ORDER_ID))
    assert gateway.calls == []


def test_evidence_refs_are_exactly_what_the_gateway_issued(trace: TraceWriter) -> None:
    gateway = FakeGateway()
    state = _collect(_case(), gateway, trace)

    assert sorted(state.evidence_refs) == sorted(gateway.issued)
    consumed = [
        ref
        for event in _events(trace)
        if event["event_type"] == "tool_result_consumed"
        for ref in event["evidence_refs"]
    ]
    assert sorted(consumed) == sorted(gateway.issued)
    state.ledger.require(state.evidence_refs)
    order = next(item for item in state.evidence if item.tool_name == "get_order")
    assert order.data["order_status"] == "canceled"
    assert order.result_hash.startswith("sha256:")


def test_missing_order_id_is_reported_not_guessed(trace: TraceWriter) -> None:
    gateway = FakeGateway()
    state = _collect(_case(order_id=None), gateway, trace)

    assert gateway.calls == []
    reasons = {(entry.tool_name, entry.reason) for entry in state.missing}
    assert ("get_order", MissingReason.ID_NOT_IN_CASE) in reasons
    assert ("get_payment", MissingReason.ID_NOT_IN_CASE) in reasons
    assert ("get_seller", MissingReason.PARAM_UNAVAILABLE) in reasons
    assert state.evidence_refs == ()


def test_transient_errors_retry_twice_then_give_up(trace: TraceWriter) -> None:
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    gateway = FakeGateway(errors={"get_payment": [httpx2.ConnectError("down") for _ in range(5)]})
    state = asyncio.run(
        collect_evidence(
            _case(), gateway, trace, retry=RetryPolicy(sleep=record_sleep), guard=CrossCaseGuard()
        )
    )

    assert [tool for tool, _, _ in gateway.calls].count("get_payment") == 3
    assert delays == [0.5, 1.0]
    assert any(
        entry.tool_name == "get_payment" and entry.reason is MissingReason.TRANSIENT_EXHAUSTED
        for entry in state.missing
    )


def test_transient_error_then_success_is_used(trace: TraceWriter) -> None:
    gateway = FakeGateway(
        errors={"get_shipment": [MCPError(CONNECTION_CLOSED, "Connection closed")]}
    )
    state = _collect(_case(), gateway, trace)

    assert [tool for tool, _, _ in gateway.calls].count("get_shipment") == 2
    assert "shipment" in state.evidence_by_domain
    assert not any(entry.tool_name == "get_shipment" for entry in state.missing)


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        ("MCP tool get_order failed: order not found", MissingReason.NOT_FOUND),
        ("MCP tool get_order failed: 403 Forbidden for this case", MissingReason.FORBIDDEN),
    ],
)
def test_not_found_and_forbidden_are_never_retried(
    trace: TraceWriter, message: str, reason: MissingReason
) -> None:
    gateway = FakeGateway(errors={"get_order": [RuntimeError(message)] * 5})
    state = _collect(_case(), gateway, trace)

    assert [tool for tool, _, _ in gateway.calls].count("get_order") == 1
    assert any(e.tool_name == "get_order" and e.reason is reason for e in state.missing)


def test_same_tool_and_arguments_are_called_once(trace: TraceWriter) -> None:
    def data(tool: str, args: dict[str, str]) -> Any:
        if tool == "get_order":
            return {
                "order_id": args["order_id"],
                "items": [
                    {"order_item_id": 1, "seller_id": "seller-aaa"},
                    {"order_item_id": 2, "seller_id": "seller-aaa"},
                ],
            }
        return {}

    gateway = FakeGateway(data=data)
    _collect(_case(), gateway, trace)

    keys = [(tool, tuple(sorted(args.items()))) for tool, _, args in gateway.calls]
    assert len(keys) == len(set(keys))


def test_follow_up_round_uses_ids_found_by_another_specialist(trace: TraceWriter) -> None:
    tools = {**DEFAULT_TOOLS, "get_shipment": _schema("shipment_id")}

    def data(tool: str, args: dict[str, str]) -> Any:
        if tool == "get_order":
            return {
                "order_id": args["order_id"],
                "shipment_id": "shp-1",
                "items": [{"order_item_id": 1, "seller_id": "seller-aaa"}],
            }
        return {"echo": args}

    gateway = FakeGateway(tools=tools, data=data)
    state = _collect(_case(), gateway, trace)

    assert ("get_shipment", CASE_ID, {"shipment_id": "shp-1"}) in gateway.calls
    follow_ups = [
        event
        for event in _events(trace)
        if event["event_type"] == "task_assigned" and event.get("decision_code") == "follow_up"
    ]
    assert [event["target"] for event in follow_ups] == [Actor.SHIPMENT.value]
    assert not state.missing


def test_undiscovered_tool_and_foreign_domain_are_not_used(trace: TraceWriter) -> None:
    tools = {name: spec for name, spec in DEFAULT_TOOLS.items() if name != "get_shipment"}
    gateway = FakeGateway(tools=tools, domains={**DOMAINS, "get_payment": "order"})
    state = _collect(_case(), gateway, trace)

    assert "get_shipment" not in {tool for tool, _, _ in gateway.calls}
    reasons = {(entry.tool_name, entry.reason) for entry in state.missing}
    assert ("get_shipment", MissingReason.TOOL_NOT_DISCOVERED) in reasons
    assert ("get_payment", MissingReason.DOMAIN_MISMATCH) in reasons
    assert all(item.tool_name != "get_payment" for item in state.evidence)


def test_trace_order_and_schema(trace: TraceWriter, contracts: Contracts) -> None:
    _collect(_case(), FakeGateway(), trace)
    events = _events(trace)
    for number, event in enumerate(events, 1):
        contracts.validate_trace(event, f"event {number}")

    types = [event["event_type"] for event in events]
    assert "case_received" not in types and "case_finalized" not in types
    assert set(types) == {"task_assigned", "tool_result_consumed", "handoff"}

    specialists = {Actor.ORDER_ITEM.value, Actor.PAYMENT.value, Actor.SHIPMENT.value}
    for actor in specialists:
        own = [i for i, e in enumerate(events) if actor in (e["actor"], e.get("target"))]
        kinds = [events[i]["event_type"] for i in own]
        assert kinds[0] == "task_assigned"
        assert kinds[-1] == "handoff"
        assert kinds.count("handoff") == 1
        assert "tool_result_consumed" in kinds

    for event in events:
        if event["event_type"] == "task_assigned":
            assert event["actor"] == "coordinator" and event["target"] in specialists
        if event["event_type"] == "tool_result_consumed":
            assert event["actor"] in specialists and len(event["evidence_refs"]) == 1
            assert event["tool_name"] in TOOL_GRANTS[Actor(event["actor"])]
        if event["event_type"] == "handoff":
            assert event["target"] == "policy-agent"


def test_order_id_conflict_is_recorded(trace: TraceWriter) -> None:
    def data(tool: str, args: dict[str, str]) -> Any:
        return {"order_id": "another-order", "items": []} if tool == "get_order" else {}

    state = _collect(_case(), FakeGateway(data=data), trace)
    assert [conflict.field for conflict in state.conflicts] == ["order_id"]
    assert state.conflicts[0].to_output()["selected_source"] is None


def test_evidence_ref_cannot_serve_two_cases(trace: TraceWriter) -> None:
    guard = CrossCaseGuard()
    guard.claim("L3A_CASE_001", "ev_" + "a" * 32)
    with pytest.raises(ValueError, match="already belongs"):
        guard.claim("L3A_CASE_002", "ev_" + "a" * 32)


def test_toolbox_errors_carry_the_reason() -> None:
    failure = ToolCallFailure(MissingReason.NOT_FOUND, "get_order", "order not found")
    assert failure.reason is MissingReason.NOT_FOUND
    assert "get_order" in str(failure)


def test_solve_case_stops_before_policy(trace: TraceWriter) -> None:
    gateway = FakeGateway()
    with pytest.raises(NotImplementedError, match="Pha 4"):
        asyncio.run(solve_case(_case(case_id="L3A_CASE_077"), gateway, trace))
    assert {case_id for _, case_id, _ in gateway.calls} == {"L3A_CASE_077"}
