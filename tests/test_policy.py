from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from fakes import ITEM_ID, ORDER_ID, SELLER_ID, FakeGateway, case
from student_agent.agents import coordinator
from student_agent.agents.coordinator import TransientCaseError, run_case
from student_agent.agents.toolbox import RetryPolicy
from student_agent.contracts import Contracts
from student_agent.state import CrossCaseGuard
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = Contracts(ROOT / "contracts" / "schemas")


async def _no_sleep(_delay: float) -> None:
    return None


def _solve(issue: str, tmp_path: Path, **kwargs: Any) -> tuple[dict[str, Any], FakeGateway]:
    gateway = kwargs.pop("gateway", None) or FakeGateway(issue=issue)
    trace = TraceWriter(tmp_path / "trace.jsonl", CONTRACTS)
    output, _ = asyncio.run(
        run_case(
            case(issue),
            gateway,
            trace,
            retry=RetryPolicy(sleep=_no_sleep),
            guard=CrossCaseGuard(),
            **kwargs,
        )
    )
    return output, gateway


EXPECTED = {
    # issue: (case_status, refund, party_type, party_id, action)
    "canceled_order_paid": ("action_required", 79.0, "platform", None, "issue_refund"),
    "unavailable_order_paid": ("action_required", 89.0, "seller", SELLER_ID, "issue_refund"),
    "late_delivery_seller": ("action_required", 18.0, "seller", SELLER_ID, "refund_freight"),
    "late_delivery_logistics": (
        "action_required",
        16.0,
        "logistics_provider",
        None,
        "refund_freight",
    ),
    "valid_split_payment": ("no_action", 0.0, "customer", None, "document_no_action"),
    "payment_mismatch": ("action_required", 35.0, "payment_provider", None, "reconcile_payment"),
    "duplicate_charge": (
        "action_required",
        64.0,
        "payment_provider",
        None,
        "refund_duplicate_charge",
    ),
    "refund_pending": ("needs_investigation", 0.0, "payment_provider", None, "monitor_refund"),
    "refund_failed": ("action_required", 52.0, "payment_provider", None, "retry_refund"),
    "unsupported_claim": ("no_action", 0.0, "customer", None, "document_no_action"),
}


@pytest.mark.parametrize("issue", sorted(EXPECTED))
def test_each_issue_is_decided_from_evidence_despite_decoy_rows(issue: str, tmp_path: Path) -> None:
    output, gateway = _solve(issue, tmp_path)
    status, refund, party_type, party_id, action = EXPECTED[issue]

    CONTRACTS.validate_output(output, issue)
    assert output["assessment"]["primary_issue"] == issue
    assert output["assessment"]["case_status"] == status
    finance = output["financial_resolution"]
    assert finance["recommended_refund_brl"] == refund
    assert sum(line["amount_brl"] for line in finance["refund_lines"]) == refund
    assert output["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": party_type, "party_id": party_id}
    ]
    assert output["resolution_actions"] == [action]
    assert output["affected_entities"]["order_ids"] == [ORDER_ID]
    if issue in {"refund_pending", "refund_failed", "unsupported_claim"}:
        assert output["affected_entities"]["item_ids"] == []
    else:
        assert output["affected_entities"]["item_ids"] == [ITEM_ID]
    assert set(output["evidence_refs"]) <= set(gateway.issued)
    assert 0 < output["assessment"]["confidence"] < 1
    verdicts = {c["claim_id"]: c["verdict"] for c in output["claim_assessments"]}
    assert verdicts["claim-a"] == "supported"


def test_policy_example_seller_id_is_never_copied(tmp_path: Path) -> None:
    output, _ = _solve("unavailable_order_paid", tmp_path)
    assert "seller-from-policy-example" not in json.dumps(output)


def test_requested_full_refund_is_evaluated_against_the_captured_total(tmp_path: Path) -> None:
    output, _ = _solve("canceled_order_paid", tmp_path)
    claims = {claim["claim_id"]: claim for claim in output["claim_assessments"]}
    assert claims["claim-b"]["verdict"] == "supported"
    assert claims["claim-b"]["evidence_refs"]


def test_customer_framing_does_not_override_evidence(tmp_path: Path) -> None:
    gateway = FakeGateway(issue="unsupported_claim")
    trace = TraceWriter(tmp_path / "trace.jsonl", CONTRACTS)
    output, _ = asyncio.run(
        run_case(case("late_delivery_seller"), gateway, trace, guard=CrossCaseGuard())
    )
    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    verdicts = {c["claim_id"]: c["verdict"] for c in output["claim_assessments"]}
    assert verdicts == {"claim-a": "unsupported", "claim-b": "unsupported"}


def test_missing_core_evidence_gives_insufficient_evidence(tmp_path: Path) -> None:
    gateway = FakeGateway(errors={"get_order": [RuntimeError("order not found")]})
    output, _ = _solve("canceled_order_paid", tmp_path, gateway=gateway)
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0
    assert output["assessment"]["confidence"] <= 0.3


def test_logistics_freight_conflict_is_reported_and_caps_confidence(tmp_path: Path) -> None:
    output, _ = _solve("late_delivery_logistics", tmp_path)
    assert [c["field"] for c in output["data_conflicts"]] == ["freight_value"]
    assert output["assessment"]["confidence"] <= 0.85


def test_full_trace_lifecycle(tmp_path: Path) -> None:
    _solve("refund_failed", tmp_path)
    events = [
        json.loads(line) for line in (tmp_path / "trace.jsonl").read_text("utf-8").splitlines()
    ]
    for event in events:
        CONTRACTS.validate_trace(event, "trace")
    types = [event["event_type"] for event in events]
    first = {kind: types.index(kind) for kind in set(types)}
    assert (
        first["task_assigned"]
        < first["tool_result_consumed"]
        < first["handoff"]
        < first["policy_decided"]
        < first["verification_completed"]
    )
    assert types[-1] == "verification_completed"
    policy_events = [e for e in events if e["actor"] == "policy-agent"]
    assert policy_events[0]["event_type"] == "tool_result_consumed"
    assert policy_events[0]["tool_name"] == "get_policy"
    assert {e["actor"] for e in events} >= {
        "coordinator",
        "order-item-agent",
        "payment-agent",
        "shipment-agent",
        "policy-agent",
        "verifier-agent",
    }


def test_verifier_sends_back_to_policy_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}
    real_verify = coordinator.verify

    def flaky_verify(state: Any, output: Any, contracts: Any) -> list[str]:
        calls["n"] += 1
        return (
            ["refund_exceeds_captured"]
            if calls["n"] == 1
            else real_verify(state, output, contracts)
        )

    monkeypatch.setattr(coordinator, "verify", flaky_verify)
    output, _ = _solve("canceled_order_paid", tmp_path)
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    events = [
        json.loads(line) for line in (tmp_path / "trace.jsonl").read_text("utf-8").splitlines()
    ]
    assert [e["decision_code"] for e in events if e["event_type"] == "verification_completed"] == [
        "revised"
    ]
    assert any(e.get("decision_code") == "revise" for e in events)


def test_verifier_gives_up_after_one_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coordinator, "verify", lambda *_: ["schema"])
    with pytest.raises(ValueError, match="verifier rejected"):
        _solve("canceled_order_paid", tmp_path)


def test_network_loss_can_request_a_rerun(tmp_path: Path) -> None:
    gateway = FakeGateway(errors={"get_payment_timeline": [httpx2.ReadTimeout("t")] * 3})
    with pytest.raises(TransientCaseError):
        _solve("canceled_order_paid", tmp_path, gateway=gateway, allow_partial=False)


def test_solve_case_returns_a_schema_valid_output(tmp_path: Path) -> None:
    gateway = FakeGateway(issue="duplicate_charge")
    trace = TraceWriter(tmp_path / "trace.jsonl", CONTRACTS)
    output = asyncio.run(
        solve_case(case("duplicate_charge", case_id="L3A_CASE_077"), gateway, trace)
    )
    CONTRACTS.validate_output(output, "solve_case")
    assert output["case_id"] == "L3A_CASE_077"
    assert {case_id for _, case_id, _ in gateway.calls} == {"L3A_CASE_077"}
