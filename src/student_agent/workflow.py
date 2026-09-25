from __future__ import annotations

from dataclasses import dataclass, field
from re import fullmatch
from typing import Any

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

EVIDENCE_REF_PATTERN = r"^ev_[A-Za-z0-9_-]{20,96}$"

PRIMARY_ISSUES = frozenset(
    {
        "canceled_order_paid",
        "unavailable_order_paid",
        "late_delivery_seller",
        "late_delivery_logistics",
        "valid_split_payment",
        "payment_mismatch",
        "duplicate_charge",
        "refund_pending",
        "refund_failed",
        "unsupported_claim",
        "insufficient_evidence",
    }
)


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
        if any(not fullmatch(EVIDENCE_REF_PATTERN, ref) for ref in self.evidence_refs):
            raise ValueError("handoff contains an invalid evidence_ref")


@dataclass
class EvidenceLedger:
    """Case-scoped evidence index; refs are accepted only from MCP responses."""

    case_id: str
    _evidence: dict[str, dict[str, Any]] = field(default_factory=dict)

    def record(self, evidence: dict[str, Any]) -> str:
        evidence_ref = evidence.get("evidence_ref")
        if not isinstance(evidence_ref, str) or not fullmatch(EVIDENCE_REF_PATTERN, evidence_ref):
            raise ValueError("MCP response has no valid evidence_ref")
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


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _money(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _emit_handoff(trace: TraceWriter, handoff: Handoff) -> None:
    trace.emit(
        case_id=handoff.case_id,
        event_type="task_assigned",
        actor=handoff.source,
        target=handoff.target,
        decision_code=handoff.task,
        evidence_refs=list(handoff.evidence_refs) or None,
        attributes={"attempt": handoff.attempt},
    )
    trace.emit(
        case_id=handoff.case_id,
        event_type="handoff",
        actor=handoff.source,
        target=handoff.target,
        decision_code=handoff.task,
        evidence_refs=list(handoff.evidence_refs) or None,
    )


async def _consume(
    gateway: EvidenceGateway,
    trace: TraceWriter,
    ledger: EvidenceLedger,
    *,
    actor: str,
    tool_name: str,
    case_id: str,
    **arguments: str,
) -> dict[str, Any]:
    evidence = await gateway.call(tool_name, case_id=case_id, **arguments)
    ref = ledger.record(evidence)
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor=actor,
        tool_name=tool_name,
        evidence_refs=[ref],
    )
    return evidence


async def _consume_optional(
    gateway: EvidenceGateway,
    trace: TraceWriter,
    ledger: EvidenceLedger,
    *,
    actor: str,
    tool_name: str,
    case_id: str,
    **arguments: str,
) -> dict[str, Any] | None:
    try:
        return await _consume(
            gateway,
            trace,
            ledger,
            actor=actor,
            tool_name=tool_name,
            case_id=case_id,
            **arguments,
        )
    except Exception:
        return None


def _infer_primary_issue(  # noqa: PLR0912, PLR0915
    order: dict[str, Any] | None,
    payments: list[dict[str, Any]],
    payment_timeline: dict[str, Any] | None,
    shipment: dict[str, Any] | None,
    refund: dict[str, Any] | None,
    claim_topics: list[str],
) -> tuple[str, float]:
    """Claim-first scoring: each claim topic is scored against MCP evidence.

    The highest-scoring topic wins.  Pure evidence inference is the fallback
    when no claim topic maps to a known primary issue.
    """
    status = str((order or {}).get("order_status", "")).lower()
    paid_total = sum(_money(row.get("payment_value")) for row in payments)

    ship_events = _as_list((shipment or {}).get("events"))
    refund_events = _as_list((refund or {}).get("events")) if refund else []
    pay_events = _as_list((payment_timeline or {}).get("events")) if payment_timeline else []

    delivered = (order or {}).get("order_delivered_customer_date")
    estimated = (order or {}).get("order_estimated_delivery_date")
    is_late_by_date = bool(delivered and estimated and str(delivered) > str(estimated))

    has_seller_late = any(
        ev.get("event_type", "").lower() in {"delivered_late", "late_delivery"}
        and "seller" in ev.get("actor", "").lower()
        for ev in ship_events
    )
    has_logistics_late = any(
        ev.get("event_type", "").lower() in {"delivered_late", "late_delivery"}
        and ev.get("actor", "").lower() in {"logistics", "carrier", "logistics_provider"}
        for ev in ship_events
    )
    has_refund_failed = any(
        "fail" in str(ev.get("status", "")).lower()
        or "fail" in str(ev.get("event_type", "")).lower()
        for ev in refund_events
    )
    has_refund_pending = any(
        "pending" in str(ev.get("status", "")).lower()
        or "pending" in str(ev.get("event_type", "")).lower()
        for ev in refund_events
    )

    amounts = [_money(row.get("payment_value")) for row in payments]
    seqs = [str(row.get("payment_sequential", "")) for row in payments]
    ptypes = [str(row.get("payment_type", "")) for row in payments]
    unique_amounts = set(amounts)
    is_duplicate = len(amounts) >= 2 and len(unique_amounts) == 1 and amounts[0] > 0
    is_split = len(set(seqs)) > 1 or len(set(ptypes)) > 1
    # mismatch: different amounts but NOT a legitimate split by seq/type
    is_mismatch = len(amounts) >= 2 and len(unique_amounts) > 1 and not is_split

    # --- score each claim topic against evidence ---
    scores: dict[str, float] = {}
    for topic in claim_topics:
        if topic == "requested_full_refund":
            continue  # never a primary_issue itself
        if topic not in PRIMARY_ISSUES:
            continue

        if topic == "canceled_order_paid":
            if status == "canceled" and paid_total > 0:
                score = 0.93
            elif status == "canceled":
                score = 0.72
            else:
                score = 0.18  # contradicted
        elif topic == "unavailable_order_paid":
            if status == "unavailable" and paid_total > 0:
                score = 0.93
            elif status == "unavailable":
                score = 0.72
            else:
                score = 0.18
        elif topic == "late_delivery_seller":
            if has_seller_late:
                score = 0.93
            elif is_late_by_date and status == "delivered":
                score = 0.80
            elif is_late_by_date:
                score = 0.66
            else:
                score = 0.44
        elif topic == "late_delivery_logistics":
            if has_logistics_late:
                score = 0.93
            elif is_late_by_date and not has_seller_late:
                score = 0.72
            elif is_late_by_date:
                score = 0.58
            else:
                score = 0.40
        elif topic == "valid_split_payment":
            if is_split and paid_total > 0:
                score = 0.90
            elif len(amounts) >= 2 and len(unique_amounts) > 1:
                score = 0.78
            elif len(amounts) >= 2:
                score = 0.65
            else:
                score = 0.55
        elif topic == "payment_mismatch":
            if is_mismatch:
                score = 0.88
            elif len(pay_events) > 0 and len(amounts) >= 2:
                score = 0.75
            elif len(amounts) >= 2:
                score = 0.65
            else:
                score = 0.55
        elif topic == "duplicate_charge":
            if is_duplicate:
                score = 0.93
            elif len(amounts) >= 2:
                score = 0.52
            else:
                score = 0.38
        elif topic == "refund_pending":
            if has_refund_pending:
                score = 0.92
            elif refund_events:
                score = 0.70
            else:
                score = 0.52
        elif topic == "refund_failed":
            if has_refund_failed:
                score = 0.92
            elif refund_events:
                score = 0.68
            else:
                score = 0.48
        elif topic == "unsupported_claim":
            # claim is literally "cannot be supported" — no contradicting hard facts needed
            if status in {"canceled", "unavailable"} and paid_total > 0:
                score = 0.30  # there IS a real issue; customer misidentified it
            elif has_seller_late or has_logistics_late:
                score = 0.35
            else:
                score = 0.72
        elif topic == "insufficient_evidence":
            score = 0.50
        else:
            score = 0.50

        scores[topic] = score

    if scores:
        best = max(scores, key=scores.__getitem__)
        return best, min(round(scores[best], 4), 0.95)

    # --- fallback: pure evidence inference when no claim topics mapped ---
    if status == "canceled" and paid_total > 0:
        return "canceled_order_paid", 0.88
    if status == "unavailable" and paid_total > 0:
        return "unavailable_order_paid", 0.88
    if has_seller_late:
        return "late_delivery_seller", 0.85
    if has_logistics_late:
        return "late_delivery_logistics", 0.85
    if is_late_by_date:
        return "late_delivery_seller", 0.62
    if has_refund_failed:
        return "refund_failed", 0.82
    if has_refund_pending:
        return "refund_pending", 0.80
    if is_duplicate:
        return "duplicate_charge", 0.84
    return "insufficient_evidence", 0.35


def _policy_rule(policy_data: dict[str, Any] | None, primary_issue: str) -> dict[str, Any]:
    rules = (policy_data or {}).get("rules") or {}
    rule = rules.get(primary_issue)
    return rule if isinstance(rule, dict) else {}


def _build_claim_assessments(
    claims: list[dict[str, Any]],
    primary_issue: str,
    primary_confidence: float,
    evidence_refs: list[str],
    refund_amount: float,
) -> list[dict[str, Any]]:
    assessments: list[dict[str, Any]] = []
    for claim in claims[:5]:
        claim_id = str(claim.get("claim_id", ""))
        topic = str(claim.get("topic", ""))
        if not claim_id:
            continue
        if topic == primary_issue:
            verdict = "supported"
            confidence = round(min(primary_confidence + 0.02, 0.95), 4)
        elif topic == "requested_full_refund":
            if refund_amount > 0:
                verdict = "supported"
                confidence = round(min(primary_confidence * 0.85, 0.88), 4)
            else:
                verdict = "partially_supported"
                confidence = 0.45
        elif topic in PRIMARY_ISSUES:
            # A competing claim that we decided against
            verdict = "unsupported"
            confidence = round(max(1.0 - primary_confidence, 0.45), 4)
        else:
            verdict = "insufficient_evidence"
            confidence = 0.40
        assessments.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": evidence_refs[:30],
            }
        )
    return assessments


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinator + specialist agents gather MCP evidence and emit an L3A output."""
    case_id = str(case["case_id"])
    request = case.get("customer_request") or {}
    order_id = str(request.get("claimed_order_id") or "").strip()
    policy_version = str(case.get("policy_version") or "EC_POLICY_V1")
    claims = _as_list(request.get("claims"))
    claim_topics = [str(claim.get("topic", "")) for claim in claims]
    ledger = EvidenceLedger(case_id=case_id)

    if not order_id:
        trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor="verifier-agent",
            decision_code="missing_order_id",
        )
        return {
            "schema_version": "day09-l3a-output-v2",
            "case_id": case_id,
            "assessment": {
                "primary_issue": "insufficient_evidence",
                "case_status": "needs_investigation",
                "confidence": 0.2,
            },
            "affected_entities": {
                "order_ids": [],
                "item_ids": [],
                "seller_ids": [],
                "payment_references": [],
                "shipment_ids": [],
            },
            "claim_assessments": _build_claim_assessments(claims, "insufficient_evidence", [], 0.0),
            "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
            "evidence_refs": [],
            "data_conflicts": [],
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": 0.0,
                "refund_lines": [],
            },
            "resolution_actions": ["request_order_identifier"],
        }

    # --- order / item specialist ---
    order_handoff = Handoff(
        case_id=case_id,
        source="coordinator",
        target="order-item-agent",
        task="collect_order_and_items",
        entity_ids=(order_id,),
    )
    _emit_handoff(trace, order_handoff)
    order_ev = await _consume_optional(
        gateway,
        trace,
        ledger,
        actor="order-item-agent",
        tool_name="get_order",
        case_id=case_id,
        order_id=order_id,
    )
    items_ev = await _consume_optional(
        gateway,
        trace,
        ledger,
        actor="order-item-agent",
        tool_name="get_order_items",
        case_id=case_id,
        order_id=order_id,
    )
    sellers_ev = await _consume_optional(
        gateway,
        trace,
        ledger,
        actor="order-item-agent",
        tool_name="get_sellers",
        case_id=case_id,
        order_id=order_id,
    )

    # --- payment specialist ---
    payment_handoff = Handoff(
        case_id=case_id,
        source="order-item-agent",
        target="payment-agent",
        task="collect_payments",
        entity_ids=(order_id,),
        evidence_refs=(order_ev["evidence_ref"],) if order_ev else (),
    )
    _emit_handoff(trace, payment_handoff)
    payments_ev = await _consume_optional(
        gateway,
        trace,
        ledger,
        actor="payment-agent",
        tool_name="get_order_payments",
        case_id=case_id,
        order_id=order_id,
    )
    pay_timeline_ev = await _consume_optional(
        gateway,
        trace,
        ledger,
        actor="payment-agent",
        tool_name="get_payment_timeline",
        case_id=case_id,
        order_id=order_id,
    )
    refund_ev = await _consume_optional(
        gateway,
        trace,
        ledger,
        actor="payment-agent",
        tool_name="get_refund_timeline",
        case_id=case_id,
        order_id=order_id,
    )

    # --- shipment specialist ---
    shipment_handoff = Handoff(
        case_id=case_id,
        source="payment-agent",
        target="shipment-agent",
        task="collect_shipment",
        entity_ids=(order_id,),
    )
    _emit_handoff(trace, shipment_handoff)
    shipment_ev = await _consume_optional(
        gateway,
        trace,
        ledger,
        actor="shipment-agent",
        tool_name="get_shipment_summary",
        case_id=case_id,
        order_id=order_id,
    )

    # --- policy specialist ---
    policy_handoff = Handoff(
        case_id=case_id,
        source="shipment-agent",
        target="policy-agent",
        task="apply_policy",
        entity_ids=(order_id,),
    )
    _emit_handoff(trace, policy_handoff)
    policy_ev = await _consume_optional(
        gateway,
        trace,
        ledger,
        actor="policy-agent",
        tool_name="get_policy",
        case_id=case_id,
        policy_version=policy_version,
    )

    order_data = (
        order_ev.get("data") if order_ev and isinstance(order_ev.get("data"), dict) else {}
    )
    items = (
        [row for row in _as_list(items_ev.get("data")) if isinstance(row, dict)]
        if items_ev
        else []
    )
    payments = (
        [row for row in _as_list(payments_ev.get("data")) if isinstance(row, dict)]
        if payments_ev
        else []
    )
    sellers = (
        [row for row in _as_list(sellers_ev.get("data")) if isinstance(row, dict)]
        if sellers_ev
        else []
    )
    shipment = (
        shipment_ev.get("data")
        if shipment_ev and isinstance(shipment_ev.get("data"), dict)
        else {}
    )
    refund = refund_ev.get("data") if refund_ev and isinstance(refund_ev.get("data"), dict) else None
    pay_timeline = (
        pay_timeline_ev.get("data")
        if pay_timeline_ev and isinstance(pay_timeline_ev.get("data"), dict)
        else None
    )
    policy_data = (
        policy_ev.get("data") if policy_ev and isinstance(policy_ev.get("data"), dict) else {}
    )

    primary_issue, confidence = _infer_primary_issue(
        order_data, payments, pay_timeline, shipment, refund, claim_topics
    )
    rule = _policy_rule(policy_data, primary_issue)
    case_status = str(rule.get("case_status") or "")
    if case_status not in {"action_required", "no_action", "needs_investigation"}:
        # sensible defaults when policy has no rule for this issue
        if primary_issue in {"unsupported_claim", "insufficient_evidence"}:
            case_status = "no_action"
        elif primary_issue == "valid_split_payment":
            case_status = "no_action"
        else:
            case_status = "needs_investigation"

    refund_amount = _money(rule.get("refund_brl"))
    if primary_issue in {"canceled_order_paid", "unavailable_order_paid"} and refund_amount <= 0:
        refund_amount = sum(_money(row.get("payment_value")) for row in payments)

    recommended_action = str(rule.get("recommended_action") or "").strip()
    resolution_actions = _unique(
        [action for action in [recommended_action, "notify_customer"] if action]
    )[:8]

    responsible = []
    for party in _as_list(rule.get("responsible_parties")):
        if not isinstance(party, dict):
            continue
        party_type = party.get("party_type")
        if party_type not in {
            "seller",
            "platform",
            "logistics_provider",
            "payment_provider",
            "customer",
            "unknown",
        }:
            continue
        responsible.append(
            {
                "party_type": party_type,
                "party_id": party.get("party_id"),
            }
        )
    if not responsible and sellers:
        responsible.append({"party_type": "seller", "party_id": sellers[0].get("seller_id")})

    evidence_refs = list(ledger.refs)[:30]
    ledger.require(evidence_refs)

    item_ids = _unique([str(row.get("order_item_id")) for row in items if row.get("order_item_id")])
    seller_ids = _unique(
        [str(row.get("seller_id")) for row in items + sellers if row.get("seller_id")]
    )
    payment_references = _unique(
        [
            f"{order_id}:seq:{row.get('payment_sequential')}:val:{row.get('payment_value')}"
            for row in payments
        ]
    )
    shipment_ids = [order_id] if shipment else []

    data_conflicts: list[dict[str, Any]] = []
    order_status = order_data.get("order_status")
    ship_status = shipment.get("order_status")
    if order_status and ship_status and order_status != ship_status:
        data_conflicts.append(
            {
                "field": "order_status",
                "sources": ["get_order", "get_shipment_summary"],
                "selected_source": "get_order",
                "resolution_code": "prefer_order_master",
            }
        )

    cause_code = primary_issue.upper()
    output = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": [order_id],
            "item_ids": item_ids,
            "seller_ids": seller_ids,
            "payment_references": payment_references,
            "shipment_ids": shipment_ids,
        },
        "claim_assessments": _build_claim_assessments(
            claims, primary_issue, confidence, evidence_refs, refund_amount
        ),
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": cause_code, "rank": 1}],
            "responsible_parties": responsible[:5],
        },
        "evidence_refs": evidence_refs,
        "data_conflicts": data_conflicts[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund_amount,
            "refund_lines": (
                [
                    {
                        "reason_code": primary_issue,
                        "amount_brl": refund_amount,
                        "entity_id": order_id,
                    }
                ]
                if refund_amount > 0
                else []
            ),
        },
        "resolution_actions": resolution_actions or ["monitor_case"],
    }

    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=primary_issue,
        evidence_refs=[policy_ev["evidence_ref"]] if policy_ev else None,
        attributes={"case_status": case_status, "refund_brl": refund_amount},
    )

    verifier_handoff = Handoff(
        case_id=case_id,
        source="policy-agent",
        target="verifier-agent",
        task="verify_output",
        entity_ids=(order_id,),
        evidence_refs=tuple(evidence_refs[:5]),
    )
    _emit_handoff(trace, verifier_handoff)
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier-agent",
        decision_code="output_ready",
        evidence_refs=evidence_refs[:20],
    )
    return output
