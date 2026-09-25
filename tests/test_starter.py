from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from student_agent import OUTPUT_SCHEMA_VERSION, VARIANT_ID
from student_agent.cases import CaseSet, load_case_set
from student_agent.contracts import ContractError, Contracts
from student_agent.submission import build_manifest
from student_agent.trace import TraceWriter
from student_agent.workflow import EvidenceLedger, Handoff, collect_specialist_evidence, solve_case


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_load_case_set_rejects_wrong_variant(tmp_path: Path) -> None:
    write_json(
        tmp_path / "case-set.json",
        {"case_set_version": "test-v1", "variant_id": "l3b", "case_ids": ["CASE_001"]},
    )
    write_json(tmp_path / "inputs" / "CASE_001.json", {"case_id": "CASE_001"})
    with pytest.raises(ValueError, match="expected variant"):
        load_case_set(tmp_path, expected_count=1)


def test_load_case_set_accepts_exact_input_inventory(tmp_path: Path) -> None:
    case_ids = ["CASE_001", "CASE_002"]
    write_json(
        tmp_path / "case-set.json",
        {"case_set_version": "test-v1", "variant_id": VARIANT_ID, "case_ids": case_ids},
    )
    for case_id in case_ids:
        write_json(tmp_path / "inputs" / f"{case_id}.json", {"case_id": case_id})
    loaded = load_case_set(tmp_path, expected_count=2)
    assert loaded.case_ids == tuple(case_ids)


def test_generated_manifest_matches_public_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    case_set = CaseSet("test-v1", VARIANT_ID, ("CASE_001",), {})
    manifest = build_manifest(case_set)
    contracts.validate_manifest(manifest)
    assert manifest["output_schema_version"] == OUTPUT_SCHEMA_VERSION


def test_contracts_require_all_public_schemas(tmp_path: Path) -> None:
    schema_root = tmp_path / "schemas"
    schema_root.mkdir()
    (schema_root / "l3a-output-v2.schema.json").write_text(
        '{"$schema":"https://json-schema.org/draft/2020-12/schema",'
        '"$id":"https://example.test/output"}',
        encoding="utf-8",
    )
    with pytest.raises(ContractError, match="missing public contract schema"):
        Contracts(schema_root)


def test_evidence_ledger_is_case_scoped() -> None:
    root = Path(__file__).resolve().parents[1]
    ledger = EvidenceLedger("L3A_CASE_001", Contracts(root / "contracts" / "schemas"))
    evidence_ref = "ev_12345678901234567890"
    evidence = {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": evidence_ref,
        "result_hash": "sha256:" + "0" * 64,
        "domain": "order",
        "data": {},
    }
    assert ledger.record(evidence) == evidence_ref
    ledger.require([evidence_ref])
    evidence["evidence_ref"] = "fake-ref"
    with pytest.raises(ValueError, match="does not match"):
        ledger.record(evidence)


def test_handoff_rejects_unregistered_evidence_ref() -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    handoff = Handoff(
        case_id="L3A_CASE_001",
        source="coordinator",
        target="payment-agent",
        task="payment_claim_verification",
        evidence_refs=("fake-ref",),
    )
    with pytest.raises(ContractError, match="does not match"):
        handoff.validate(contracts)


def test_specialists_propagate_case_id_and_trace_consumed_evidence(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")

    class FakeGateway:
        def __init__(self) -> None:
            self.contracts = contracts
            self.calls: list[tuple[str, str, dict[str, str]]] = []

        async def list_tools(self) -> list[str]:
            return [
                "get_order",
                "get_order_items",
                "get_sellers",
                "get_product_context",
                "get_order_payments",
                "get_payment_timeline",
                "get_refund_timeline",
                "get_shipment_summary",
                "get_policy",
            ]

        async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict:
            self.calls.append((tool_name, case_id, arguments))
            if tool_name == "get_refund_timeline":
                raise RuntimeError("not found")
            evidence_ref = "ev_" + tool_name + "_" + "x" * 20
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": evidence_ref,
                "result_hash": "sha256:" + "0" * 64,
                "domain": "order",
                "data": {"tool": tool_name},
            }

    case = {
        "case_id": "L3A_CASE_001",
        "customer_request": {"claimed_order_id": "order-001"},
        "policy_version": "EC_POLICY_V1",
    }
    gateway = FakeGateway()
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    ledger, results = asyncio.run(collect_specialist_evidence(case, gateway, trace))

    assert gateway.calls
    assert all(case_id == case["case_id"] for _, case_id, _ in gateway.calls)
    assert len(ledger.refs) == len(gateway.calls) - 1
    assert results["payment-agent"].failures == ["get_refund_timeline: not found"]
    events = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    consumed = [event for event in events if event["event_type"] == "tool_result_consumed"]
    assert len(consumed) == len(ledger.refs)

    output = asyncio.run(solve_case(case, gateway, trace))
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    events = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    event_types = [event["event_type"] for event in events]
    assert "handoff" in event_types
    assert "policy_decided" in event_types
    assert "verification_completed" in event_types


def test_specialist_collect_handles_server_mcp_errors(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")

    class FakeGateway:
        def __init__(self) -> None:
            self.contracts = contracts

        async def list_tools(self) -> list[str]:
            return ["get_order"]

        async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict:
            raise RuntimeError("MCP tool get_order failed: upstream error")

    case = {
        "case_id": "L3A_CASE_002",
        "customer_request": {"claimed_order_id": "order-002"},
        "policy_version": "EC_POLICY_V1",
    }
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    agent = collect_specialist_evidence.__globals__["SpecialistAgent"](
        "order-item-agent", FakeGateway(), trace
    )

    result = asyncio.run(
        agent.collect(
            case_id=case["case_id"],
            requests=(collect_specialist_evidence.__globals__["ToolRequest"]("get_order", {"order_id": "order-002"}),),
            ledger=EvidenceLedger(case["case_id"], contracts),
        )
    )

    assert result.failures == ["get_order: MCP tool get_order failed: upstream error"]
