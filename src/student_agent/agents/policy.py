"""Policy agent: fetch the case's policy version, classify the primary issue from facts,
and build the l3a-output-v2 decision. Deterministic rules only."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..state import Actor, CaseState, DataConflict, KnownIds, MissingReason
from .facts import CENT, CaseFacts, extract_facts
from .specialists import Specialist, ToolStep

OUTPUT_SCHEMA_VERSION = "day09-l3a-output-v2"

ISSUES = frozenset(
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
CLAIM_TOPICS = ISSUES | {"requested_full_refund"}
COMPLETED_REFUND_NOTES = frozenset({"fully_refunded"})

# Used only when get_policy is unavailable; mirrors the structure of EC_POLICY_V1.
FALLBACK_RULES: Mapping[str, tuple[str, str, str]] = {
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
}
INSUFFICIENT_RULE = ("needs_investigation", "request_more_evidence", "unknown")

CAUSE_CODES: Mapping[str, str] = {
    "canceled_order_paid": "ORDER_CANCELED_AFTER_CAPTURE",
    "unavailable_order_paid": "ORDER_UNAVAILABLE_AFTER_CAPTURE",
    "late_delivery_seller": "SELLER_MISSED_SHIPPING_LIMIT",
    "late_delivery_logistics": "CARRIER_DELIVERY_DELAY",
    "valid_split_payment": "SPLIT_PAYMENT_MATCHES_ORDER_TOTAL",
    "payment_mismatch": "PAYMENT_RECONCILIATION_MISMATCH",
    "duplicate_charge": "DUPLICATE_PAYMENT_CAPTURE",
    "refund_pending": "REFUND_PENDING_AT_PROVIDER",
    "refund_failed": "REFUND_FAILED_AT_PROVIDER",
    "unsupported_claim": "NO_FAULT_FOUND_IN_EVIDENCE",
    "insufficient_evidence": "REQUIRED_EVIDENCE_UNAVAILABLE",
}

# Which evidence supports which conclusion (evidence precision: cite only these).
SUPPORTING_EVIDENCE: Mapping[str, tuple[str, ...]] = {
    "canceled_order_paid": ("order", "payments", "payment_timeline"),
    "unavailable_order_paid": ("order", "items", "sellers", "payments", "payment_timeline"),
    "late_delivery_seller": ("order", "items", "sellers", "shipment", "payment_timeline"),
    "late_delivery_logistics": ("order", "items", "shipment", "payment_timeline"),
    "valid_split_payment": ("items", "payments", "payment_timeline"),
    "payment_mismatch": ("payments", "payment_timeline"),
    "duplicate_charge": ("items", "payments", "payment_timeline"),
    "refund_pending": ("payment_timeline", "refund_timeline"),
    "refund_failed": ("payment_timeline", "refund_timeline"),
    "unsupported_claim": ("order", "shipment", "payment_timeline"),
    # Nothing is concluded, but cite what was actually examined.
    "insufficient_evidence": ("order", "payment_timeline", "shipment"),
}
REQUIRED_EVIDENCE = ("order", "payment_timeline")


class PolicyAgent(Specialist):
    """Only fetches the policy document; the decision itself is pure (``decide``)."""

    actor = Actor.POLICY
    plan = (ToolStep("get_policy"),)

    async def run(self, seed: KnownIds) -> None:
        version = self.case.get("policy_version")
        if isinstance(version, str) and version:
            seed = seed.copy()
            seed.add("policy_version", version)
        await super().run(seed)
        self.finish()


@dataclass
class PolicyDecision:
    primary_issue: str
    case_status: str
    action: str
    party_type: str
    party_id: str | None
    refund: float
    confidence: float
    cited: list[str]
    conflicts: list[DataConflict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    attempt: int = 1


def _rule(policy: Any, issue: str) -> tuple[str, str, str]:
    if issue == "insufficient_evidence":
        return INSUFFICIENT_RULE
    rules = policy.get("rules") if isinstance(policy, Mapping) else None
    rule = rules.get(issue) if isinstance(rules, Mapping) else None
    if isinstance(rule, Mapping):
        parties = rule.get("responsible_parties") or [{}]
        party = parties[0].get("party_type") if isinstance(parties[0], Mapping) else None
        return (
            str(rule.get("case_status") or FALLBACK_RULES[issue][0]),
            str(rule.get("recommended_action") or FALLBACK_RULES[issue][1]),
            str(party or FALLBACK_RULES[issue][2]),
        )
    return FALLBACK_RULES.get(issue, INSUFFICIENT_RULE)


def _policy_rule(policy: Any, issue: str) -> tuple[bool, float | None]:
    """Return whether the requested issue is authorized by a verified policy."""
    if not isinstance(policy, Mapping) or policy.get("currency") != "BRL":
        return False, None
    rules = policy.get("rules")
    rule = rules.get(issue) if isinstance(rules, Mapping) else None
    if not isinstance(rule, Mapping):
        return False, None
    amount = rule.get("refund_brl")
    if amount is None:
        return True, None
    try:
        numeric = float(amount)
    except (TypeError, ValueError):
        return False, None
    if numeric < 0 or numeric != numeric or numeric in (float("inf"), float("-inf")):
        return False, None
    return True, round(numeric, 2)


def classify(facts: CaseFacts) -> tuple[str, float, list[str]]:
    """Return ``(primary_issue, refund_brl, notes)`` from evidence facts only."""
    if "order" not in facts.refs or "payment_timeline" not in facts.refs:
        return "insufficient_evidence", 0.0, ["order or payment evidence missing"]
    paid = facts.captured_total
    if facts.refunds:
        latest = facts.refunds[-1]
        if latest.status.lower() == "failed":
            return "refund_failed", max(latest.amount - facts.refunded_total, 0.0), []
        if latest.status.lower() == "pending":
            return "refund_pending", 0.0, []
    if facts.order_status in {"canceled", "cancelled"} and paid > 0:
        remaining = max(paid - facts.refunded_total, 0.0)
        if remaining <= CENT:
            return "canceled_order_paid", 0.0, [*COMPLETED_REFUND_NOTES]
        return "canceled_order_paid", remaining, []
    if (
        facts.order_status
        in {
            "unavailable",
            "cancelled_no_stock",
            "canceled_no_stock",
        }
        and paid > 0
    ):
        remaining = max(paid - facts.refunded_total, 0.0)
        if remaining <= CENT:
            return "unavailable_order_paid", 0.0, [*COMPLETED_REFUND_NOTES]
        return "unavailable_order_paid", remaining, []
    if facts.order_status in {
        "canceled",
        "cancelled",
        "unavailable",
        "cancelled_no_stock",
        "canceled_no_stock",
    }:
        return "unsupported_claim", 0.0, ["order was not captured"]
    if facts.mismatches:
        return "payment_mismatch", round(sum(e.amount for e in facts.mismatches), 2), []
    repeated = [amount for amount, count in facts.captured_by_amount().items() if count >= 2]
    if len(facts.captures) >= 2:
        total = facts.order_total
        if total is not None and abs(paid - total) <= CENT:
            return "valid_split_payment", 0.0, []
    if repeated:
        return "duplicate_charge", repeated[0], []
    if facts.is_late:
        if facts.seller_missed_limit:
            return "late_delivery_seller", paid, []
        if facts.seller_missed_limit is False:
            return "late_delivery_logistics", paid, []
        return "insufficient_evidence", 0.0, ["late delivery but no shipping limit to assign fault"]
    if facts.is_late is None:
        return "insufficient_evidence", 0.0, ["delivery dates not in evidence"]
    return "unsupported_claim", 0.0, []


def _conflicts(facts: CaseFacts, issue: str) -> list[DataConflict]:
    conflicts: list[DataConflict] = list(facts.conflicts)
    freight = facts.item_freight
    if issue.startswith("late_delivery") and freight is not None and facts.captures:
        paid = facts.captured_total
        if abs(paid - freight) > CENT:
            conflicts.append(
                DataConflict(
                    field="freight_value",
                    sources=("get_order_items", "get_payment_timeline"),
                    selected_source="get_payment_timeline",
                    resolution_code="captured_amount_is_authoritative",
                    observed={
                        "get_order_items": f"{freight:.2f}",
                        "get_payment_timeline": f"{paid:.2f}",
                    },
                )
            )
    actors = set(facts.late_event_actors)
    expected = {"late_delivery_seller": "seller", "late_delivery_logistics": "logistics_provider"}
    if issue in expected and actors and expected[issue] not in actors:
        conflicts.append(
            DataConflict(
                field="late_delivery_actor",
                sources=("get_order_items", "get_shipment_summary"),
                selected_source="get_order_items",
                resolution_code="shipping_limit_vs_carrier_date",
            )
        )
    return conflicts[:5]


def _confidence(state: CaseState, facts: CaseFacts, issue: str, conflicts: list) -> float:
    if issue == "insufficient_evidence":
        return 0.3
    needed = set(SUPPORTING_EVIDENCE[issue]) | set(REQUIRED_EVIDENCE)
    missing = [name for name in needed if name not in facts.refs]
    score = 0.9 - 0.15 * len(missing) - 0.1 * len(conflicts)
    if "policy" not in facts.refs:
        score -= 0.1
    if not facts.policy_valid and issue not in {"unsupported_claim", "insufficient_evidence"}:
        score -= 0.15
    if any(entry.reason is MissingReason.TRANSIENT_EXHAUSTED for entry in state.missing):
        score -= 0.1
    claimed = {
        claim.get("topic")
        for claim in (state.case.get("customer_request") or {}).get("claims", [])
        if isinstance(claim, Mapping)
    }
    if issue not in claimed:
        score -= 0.1  # evidence overrules the customer's framing; keep some doubt
    return round(min(0.95, max(0.05, score)), 2)


def decide(state: CaseState, *, conservative: bool = False) -> PolicyDecision:
    facts = extract_facts(state)
    policy = next((i.data for i in state.evidence if i.tool_name == "get_policy"), None)
    issue, refund, notes = classify(facts)
    if conservative and issue != "insufficient_evidence":
        issue, refund, notes = "insufficient_evidence", 0.0, [*notes, "verifier requested review"]
    status, action, party_type = _rule(policy, issue)
    authorized, policy_cap = _policy_rule(policy, issue)
    if issue not in {"unsupported_claim", "insufficient_evidence"} and not authorized:
        status, action, party_type = INSUFFICIENT_RULE
        refund = 0.0
        notes = [*notes, "policy unavailable or does not authorize this issue"]
    if "fully_refunded" in notes:
        status, action = "no_action", "document_no_action"
        refund = 0.0
    if status != "action_required":
        refund = 0.0
    party_id = None
    if party_type == "seller":
        party_id = facts.seller_ids[0] if len(facts.seller_ids) == 1 else None
    conflicts = _conflicts(facts, issue)
    cited = [facts.refs[name] for name in SUPPORTING_EVIDENCE[issue] if name in facts.refs]
    if issue != "insufficient_evidence" and "policy" in facts.refs:
        cited.append(facts.refs["policy"])
    return PolicyDecision(
        primary_issue=issue,
        case_status=status,
        action=action,
        party_type=party_type,
        party_id=party_id,
        refund=round(refund, 2),
        confidence=_confidence(state, facts, issue, conflicts),
        cited=list(dict.fromkeys(cited)),
        conflicts=conflicts,
        notes=notes,
    )


def _topic_refs(topic: str, facts: CaseFacts) -> list[str]:
    groups: Mapping[str, tuple[str, ...]] = {
        "canceled_order_paid": ("order", "payments", "payment_timeline"),
        "unavailable_order_paid": ("order", "items", "sellers", "payments", "payment_timeline"),
        "late_delivery_seller": ("order", "items", "sellers", "shipment"),
        "late_delivery_logistics": ("order", "items", "shipment"),
        "valid_split_payment": ("items", "payments", "payment_timeline"),
        "payment_mismatch": ("payments", "payment_timeline"),
        "duplicate_charge": ("payments", "payment_timeline"),
        "refund_pending": ("payment_timeline", "refund_timeline"),
        "refund_failed": ("payment_timeline", "refund_timeline"),
        "requested_full_refund": ("order", "payments", "payment_timeline", "refund_timeline"),
        "unsupported_claim": ("order", "shipment", "payment_timeline"),
    }
    return list(
        dict.fromkeys(facts.refs[name] for name in groups.get(topic, ()) if name in facts.refs)
    )


def _topic_supported(topic: str, facts: CaseFacts) -> bool | None:
    """Return True/False, or None when the topic cannot be decided from evidence."""
    paid = facts.captured_total
    if topic == "canceled_order_paid":
        return facts.order_status in {"canceled", "cancelled"} and paid > 0
    if topic == "unavailable_order_paid":
        return (
            facts.order_status
            in {
                "unavailable",
                "cancelled_no_stock",
                "canceled_no_stock",
            }
            and paid > 0
        )
    if topic == "late_delivery_seller":
        if facts.is_late is None or facts.seller_missed_limit is None:
            return None
        return facts.is_late and facts.seller_missed_limit
    if topic == "late_delivery_logistics":
        if facts.is_late is None or facts.seller_missed_limit is None:
            return None
        return facts.is_late and not facts.seller_missed_limit
    if topic == "valid_split_payment":
        total = facts.order_total
        return (
            len(facts.captures) >= 2
            and total is not None
            and abs(facts.captured_total - total) <= CENT
        )
    if topic == "payment_mismatch":
        return bool(facts.mismatches)
    if topic == "duplicate_charge":
        repeated = [count for count in facts.captured_by_amount().values() if count >= 2]
        if not repeated:
            return False
        total = facts.order_total
        return total is None or abs(facts.captured_total - total) > CENT
    if topic == "refund_pending":
        return bool(facts.refunds and facts.refunds[-1].status.lower() == "pending")
    if topic == "refund_failed":
        return bool(facts.refunds and facts.refunds[-1].status.lower() == "failed")
    if topic == "requested_full_refund":
        return facts.captured_total > 0
    if topic == "unsupported_claim":
        return classify(facts)[0] == "unsupported_claim"
    return None


def _claim_assessment(
    topic: str, facts: CaseFacts, decision: PolicyDecision
) -> tuple[str, list[str]]:
    if topic not in CLAIM_TOPICS:
        return "insufficient_evidence", []
    refs = _topic_refs(topic, facts)
    required = {
        "canceled_order_paid": ("order", "payment_timeline"),
        "unavailable_order_paid": ("order", "items", "payment_timeline"),
        "late_delivery_seller": ("order", "items", "shipment"),
        "late_delivery_logistics": ("order", "items", "shipment"),
        "valid_split_payment": ("items", "payment_timeline"),
        "payment_mismatch": ("payment_timeline",),
        "duplicate_charge": ("payment_timeline",),
        "refund_pending": ("payment_timeline", "refund_timeline"),
        "refund_failed": ("payment_timeline", "refund_timeline"),
        "requested_full_refund": ("order", "payment_timeline"),
        "unsupported_claim": ("order", "shipment", "payment_timeline"),
    }[topic]
    if any(name not in facts.refs for name in required):
        return "insufficient_evidence", refs
    if topic == "requested_full_refund":
        if facts.refunded_total >= facts.captured_total - CENT and facts.captured_total > 0:
            return "supported", refs
        if decision.refund > 0:
            verdict = (
                "supported"
                if decision.refund >= facts.captured_total - CENT
                else "partially_supported"
            )
            return verdict, refs
        return "unsupported", refs
    supported = _topic_supported(topic, facts)
    if supported is None:
        return "insufficient_evidence", refs
    return ("supported" if supported else "unsupported"), refs


def build_output(state: CaseState, decision: PolicyDecision) -> dict[str, Any]:
    facts = extract_facts(state)
    entities = state.entities.to_output()
    if facts.order_id:
        entities["order_ids"] = [facts.order_id]
    if facts.items:
        entities["item_ids"] = list(
            dict.fromkeys(str(i["order_item_id"]) for i in facts.items if i.get("order_item_id"))
        )
        entities["seller_ids"] = facts.seller_ids
    entities["payment_references"] = []  # gateway rows carry no payment identifier
    claims = [
        claim
        for claim in (state.case.get("customer_request") or {}).get("claims", [])
        if isinstance(claim, Mapping) and claim.get("claim_id")
    ][:5]
    refund_lines = []
    if decision.refund > 0:
        refund_lines.append(
            {
                "reason_code": decision.primary_issue,
                "amount_brl": decision.refund,
                "entity_id": facts.order_id,
            }
        )
    claim_assessments = []
    for claim in claims:
        topic = claim.get("topic") if isinstance(claim.get("topic"), str) else ""
        verdict, refs = _claim_assessment(topic, facts, decision)
        claim_assessments.append(
            {
                "claim_id": str(claim["claim_id"])[:64],
                "verdict": verdict,
                "confidence": decision.confidence
                if verdict != "insufficient_evidence"
                else min(decision.confidence, 0.3),
                "evidence_refs": refs,
            }
        )
    claim_refs = [ref for claim in claim_assessments for ref in claim["evidence_refs"]]
    cited = list(dict.fromkeys([*decision.cited, *claim_refs]))
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": state.case_id,
        "assessment": {
            "primary_issue": decision.primary_issue,
            "case_status": decision.case_status,
            "confidence": decision.confidence,
        },
        "affected_entities": entities,
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": CAUSE_CODES[decision.primary_issue], "rank": 1}],
            "responsible_parties": [
                {"party_type": decision.party_type, "party_id": decision.party_id}
            ],
        },
        "evidence_refs": cited,
        "data_conflicts": [conflict.to_output() for conflict in decision.conflicts],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": decision.refund,
            "refund_lines": refund_lines,
        },
        "resolution_actions": [decision.action],
    }
