from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from .contracts import Contracts
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

AGENT_ROLES = (
    "coordinator",
    "order-item-agent",
    "payment-agent",
    "shipment-agent",
    "policy-agent",
    "verifier-agent",
)

RESPONSIBLE_PARTIES_BY_ISSUE = {
    "canceled_order_paid": {"platform"},
    "unavailable_order_paid": {"seller"},
    "late_delivery_seller": {"seller"},
    "late_delivery_logistics": {"logistics_provider"},
    "payment_mismatch": {"payment_provider"},
    "duplicate_charge": {"payment_provider"},
    "refund_pending": {"payment_provider"},
    "refund_failed": {"payment_provider"},
    "valid_split_payment": {"customer"},
    "unsupported_claim": {"customer"},
}


@dataclass(frozen=True)
class Handoff:
    """Observable A2A envelope passed between agents for one case."""

    case_id: str
    source: str
    target: str
    task: str
    entity_ids: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    attempt: int = 1

    def __post_init__(self) -> None:
        if self.source not in AGENT_ROLES or self.target not in AGENT_ROLES:
            raise ValueError("handoff source and target must be registered agent roles")
        if not self.case_id or not self.task or self.attempt < 1:
            raise ValueError("handoff requires case_id, task and positive attempt")

    def validate(self, contracts: Contracts) -> None:
        for evidence_ref in self.evidence_refs:
            contracts.validate_evidence_ref(evidence_ref, "handoff evidence_ref")


@dataclass
class EvidenceLedger:
    """Case-scoped evidence index; refs are accepted only from MCP responses."""

    case_id: str
    contracts: Contracts
    _evidence: dict[str, dict[str, Any]] = field(default_factory=dict)

    def record(self, evidence: dict[str, Any]) -> str:
        self.contracts.validate_evidence(evidence)
        evidence_ref = evidence.get("evidence_ref")
        if not isinstance(evidence_ref, str):
            raise ValueError("MCP response has no evidence_ref")
        existing = self._evidence.get(evidence_ref)
        if existing is not None and existing != evidence:
            raise ValueError(f"evidence_ref reused with different content: {evidence_ref}")
        self._evidence[evidence_ref] = evidence
        return evidence_ref

    def contains(self, evidence_ref: str) -> bool:
        return evidence_ref in self._evidence

    def require(self, evidence_refs: list[str] | tuple[str, ...]) -> None:
        missing = [ref for ref in evidence_refs if not self.contains(ref)]
        if missing:
            raise ValueError(f"evidence refs are not in case ledger: {missing}")

    @property
    def refs(self) -> tuple[str, ...]:
        return tuple(self._evidence)


@dataclass(frozen=True)
class ToolRequest:
    name: str
    arguments: dict[str, str]


@dataclass
class SpecialistResult:
    actor: str
    evidence: list[dict[str, Any]] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    evidence_by_tool: dict[str, dict[str, Any]] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)


class SpecialistAgent:
    def __init__(self, actor: str, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        if actor not in AGENT_ROLES:
            raise ValueError(f"unknown specialist actor: {actor}")
        self.actor = actor
        self.gateway = gateway
        self.trace = trace

    async def collect(
        self,
        *,
        case_id: str,
        requests: tuple[ToolRequest, ...],
        ledger: EvidenceLedger,
    ) -> SpecialistResult:
        result = SpecialistResult(self.actor)
        available = set(await self.gateway.list_tools())

        async def call(
            request: ToolRequest,
        ) -> tuple[ToolRequest, dict[str, Any] | None, str | None]:
            if request.name not in available:
                return request, None, f"tool unavailable: {request.name}"
            try:
                evidence = await self.gateway.call(
                    request.name, case_id=case_id, **request.arguments
                )
            except (RuntimeError, ValueError) as exc:
                return request, None, f"{request.name}: {exc}"
            return request, evidence, None

        outcomes = await asyncio.gather(*(call(request) for request in requests))
        for request, evidence, failure in outcomes:
            if failure is not None:
                result.failures.append(failure)
                continue
            if evidence is None:
                continue
            evidence_ref = ledger.record(evidence)
            result.evidence.append(evidence)
            result.evidence_refs.append(evidence_ref)
            result.evidence_by_tool[request.name] = evidence
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=self.actor,
                tool_name=request.name,
                evidence_refs=[evidence_ref],
            )
        return result


def _order_id(case: dict[str, Any]) -> str:
    customer_request = case.get("customer_request")
    if not isinstance(customer_request, dict):
        raise ValueError("case.customer_request must be an object")
    order_id = customer_request.get("claimed_order_id")
    if not isinstance(order_id, str) or not order_id:
        raise ValueError("case.customer_request.claimed_order_id is required")
    return order_id


def _policy_version(case: dict[str, Any]) -> str:
    policy_version = case.get("policy_version")
    if not isinstance(policy_version, str) or not policy_version:
        raise ValueError("case.policy_version is required")
    return policy_version


async def collect_specialist_evidence(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> tuple[EvidenceLedger, dict[str, SpecialistResult]]:
    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("case.case_id is required")
    order_id = _order_id(case)
    policy_version = _policy_version(case)
    ledger = EvidenceLedger(case_id, gateway.contracts)
    requests_by_actor = {
        "order-item-agent": (
            ToolRequest("get_order", {"order_id": order_id}),
            ToolRequest("get_order_items", {"order_id": order_id}),
            ToolRequest("get_sellers", {"order_id": order_id}),
            ToolRequest("get_product_context", {"order_id": order_id}),
        ),
        "payment-agent": (
            ToolRequest("get_order_payments", {"order_id": order_id}),
            ToolRequest("get_payment_timeline", {"order_id": order_id}),
            ToolRequest("get_refund_timeline", {"order_id": order_id}),
        ),
        "shipment-agent": (ToolRequest("get_shipment_summary", {"order_id": order_id}),),
        "policy-agent": (ToolRequest("get_policy", {"policy_version": policy_version}),),
    }
    results: dict[str, SpecialistResult] = {}
    primary_agents = {
        actor: SpecialistAgent(actor, gateway, trace)
        for actor in ("order-item-agent", "payment-agent", "shipment-agent")
    }
    for actor, requests in requests_by_actor.items():
        if actor == "policy-agent":
            continue
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=actor,
            attributes={"task_count": len(requests)},
        )
    primary_results = await asyncio.gather(
        *(
            primary_agents[actor].collect(
                case_id=case_id,
                requests=requests_by_actor[actor],
                ledger=ledger,
            )
            for actor in primary_agents
        )
    )
    results.update({result.actor: result for result in primary_results})
    primary_refs = _refs(*(ref for result in primary_results for ref in result.evidence_refs))
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="policy-agent",
        evidence_refs=primary_refs,
        attributes={"specialist_count": len(results)},
    )
    policy_requests = requests_by_actor["policy-agent"]
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="policy-agent",
        attributes={"task_count": len(policy_requests)},
    )
    policy_agent = SpecialistAgent("policy-agent", gateway, trace)
    results["policy-agent"] = await policy_agent.collect(
        case_id=case_id,
        requests=policy_requests,
        ledger=ledger,
    )
    return ledger, results


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run scoped specialists, apply the authoritative policy, then verify output."""
    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("case.case_id is required")
    ledger, results = await collect_specialist_evidence(case, gateway, trace)
    output = _build_case_output(case, results, ledger)
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=output["assessment"]["primary_issue"],
        evidence_refs=output["evidence_refs"],
    )
    VerifierAgent(gateway.contracts).verify(output, ledger, results)
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier-agent",
        decision_code="OUTPUT_SCHEMA_VALID",
        evidence_refs=output["evidence_refs"],
    )
    return output


class VerifierAgent:
    def __init__(self, contracts: Contracts) -> None:
        self.contracts = contracts

    def verify(
        self,
        output: dict[str, Any],
        ledger: EvidenceLedger,
        results: dict[str, SpecialistResult],
    ) -> None:
        self.contracts.validate_output(output, f"case {output['case_id']} output")
        evidence_refs = output["evidence_refs"]
        ledger.require(evidence_refs)
        assessment = output["assessment"]
        primary_issue = assessment["primary_issue"]
        responsible_types = {
            party["party_type"] for party in output["root_cause_analysis"]["responsible_parties"]
        }
        expected_types = RESPONSIBLE_PARTIES_BY_ISSUE.get(primary_issue, set())
        if expected_types and not responsible_types.issubset(expected_types):
            raise ValueError(
                f"responsible party mismatch for {primary_issue}: {sorted(responsible_types)}"
            )
        financial = output["financial_resolution"]
        line_total = sum(Decimal(str(line["amount_brl"])) for line in financial["refund_lines"])
        if line_total != Decimal(str(financial["recommended_refund_brl"])):
            raise ValueError("refund lines do not equal recommended_refund_brl")
        if assessment["case_status"] == "no_action" and line_total != 0:
            raise ValueError("no_action case cannot recommend a refund")
        if primary_issue != "insufficient_evidence" and not evidence_refs:
            raise ValueError("resolved issue requires supporting evidence")
        if assessment["confidence"] > _calibrated_confidence(results, evidence_refs):
            raise ValueError("confidence exceeds evidence calibration")


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _unique_strings(values: list[Any]) -> list[str]:
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value))


def _build_case_output(
    case: dict[str, Any], results: dict[str, SpecialistResult], ledger: EvidenceLedger
) -> dict[str, Any]:
    case_id = case["case_id"]
    order_id = _order_id(case)
    claims = case.get("customer_request", {}).get("claims", [])
    order_result = results["order-item-agent"]
    payment_result = results["payment-agent"]
    shipment_result = results["shipment-agent"]
    policy_result = results["policy-agent"]
    order_evidence = order_result.evidence_by_tool.get("get_order", {})
    order_data = order_evidence.get("data", {})
    payment_evidence = payment_result.evidence_by_tool.get("get_order_payments", {})
    payment_data = payment_evidence.get("data", [])
    shipment_evidence = shipment_result.evidence_by_tool.get("get_shipment_summary", {})
    shipment_data = shipment_evidence.get("data", {})
    policy_evidence = policy_result.evidence_by_tool.get("get_policy", {})
    policy_data = policy_evidence.get("data", {})
    policy_rules = policy_data.get("rules", {}) if isinstance(policy_data, dict) else {}

    order_status = order_data.get("order_status")
    captured_total = sum(
        (
            amount
            for amount in (_decimal(item.get("payment_value")) for item in payment_data)
            if amount
        ),
        Decimal("0"),
    )
    shipment_events = shipment_data.get("events", [])
    late_actor = next(
        (
            event.get("actor")
            for event in shipment_events
            if event.get("event_type") == "delivered_late"
        ),
        None,
    )

    issue_candidates: list[tuple[str, list[str]]] = []
    order_ref = order_result.evidence_by_tool.get("get_order", {}).get("evidence_ref")
    payment_ref = payment_evidence.get("evidence_ref")
    shipment_ref = shipment_evidence.get("evidence_ref")
    if order_status == "canceled" and captured_total > 0:
        issue_candidates.append(("canceled_order_paid", _refs(order_ref, payment_ref)))
    if late_actor == "seller":
        issue_candidates.append(("late_delivery_seller", _refs(shipment_ref)))
    if late_actor in {"logistics", "logistics_provider"}:
        issue_candidates.append(("late_delivery_logistics", _refs(shipment_ref)))

    requested_topics = [
        claim.get("topic")
        for claim in claims
        if isinstance(claim, dict) and isinstance(claim.get("topic"), str)
    ]
    primary_issue = next(
        (
            topic
            for topic in requested_topics
            if any(topic == candidate[0] for candidate in issue_candidates)
        ),
        "insufficient_evidence",
    )
    supporting_refs = next((refs for issue, refs in issue_candidates if issue == primary_issue), [])
    policy_rule = policy_rules.get(primary_issue, {}) if isinstance(policy_rules, dict) else {}
    policy_ref = policy_evidence.get("evidence_ref")
    if policy_rule:
        supporting_refs = _refs(*supporting_refs, policy_ref)

    claim_assessments = []
    for claim in claims:
        if not isinstance(claim, dict) or not isinstance(claim.get("claim_id"), str):
            continue
        topic = claim.get("topic")
        refs = next((refs for issue, refs in issue_candidates if issue == topic), [])
        verdict = "supported" if refs else "insufficient_evidence"
        claim_assessments.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": 0.9 if verdict == "supported" else 0.2,
                "evidence_refs": refs,
            }
        )

    items = order_result.evidence_by_tool.get("get_order_items", {}).get("data", [])
    sellers = order_result.evidence_by_tool.get("get_sellers", {}).get("data", [])
    responsible_parties = (
        policy_rule.get("responsible_parties", []) if isinstance(policy_rule, dict) else []
    )
    refund_amount = policy_rule.get("refund_brl", 0) if isinstance(policy_rule, dict) else 0
    refund_amount = float(refund_amount) if isinstance(refund_amount, (int, float)) else 0.0
    status = (
        policy_rule.get("case_status", "needs_investigation")
        if isinstance(policy_rule, dict)
        else "needs_investigation"
    )
    action = policy_rule.get("recommended_action") if isinstance(policy_rule, dict) else None
    resolution_actions = [action] if isinstance(action, str) else []
    if not resolution_actions:
        resolution_actions = ["investigate_missing_evidence"]
    confidence = _calibrated_confidence(results, supporting_refs)

    output = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "case_status": status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": [order_id],
            "item_ids": _unique_strings(
                [item.get("order_item_id") for item in items if isinstance(item, dict)]
            ),
            "seller_ids": _unique_strings(
                [item.get("seller_id") for item in items if isinstance(item, dict)]
            )
            or _unique_strings(
                [seller.get("seller_id") for seller in sellers if isinstance(seller, dict)]
            ),
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": primary_issue.upper(), "rank": 1}],
            "responsible_parties": responsible_parties,
        },
        "evidence_refs": _refs(*supporting_refs),
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund_amount,
            "refund_lines": [
                {"reason_code": primary_issue, "amount_brl": refund_amount, "entity_id": order_id}
            ]
            if refund_amount > 0
            else [],
        },
        "resolution_actions": resolution_actions,
    }
    ledger.require(output["evidence_refs"])
    return output


def _refs(*refs: Any) -> list[str]:
    return _unique_strings(list(refs))


def _calibrated_confidence(
    results: dict[str, SpecialistResult], evidence_refs: list[str]
) -> float:
    failures = sum(len(result.failures) for result in results.values())
    if not evidence_refs:
        return 0.2
    if failures:
        return 0.65 if len(evidence_refs) >= 2 else 0.4
    if len(evidence_refs) >= 3:
        return 0.9
    return 0.75
