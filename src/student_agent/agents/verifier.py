"""Verifier: cross-field invariants on the policy output before it is finalized."""

from __future__ import annotations

from typing import Any

from ..contracts import ContractError, Contracts
from ..state import CaseState, iter_id_fields
from .facts import CENT, extract_facts
from .policy import PolicyDecision

CONFLICT_CONFIDENCE_CAP = 0.85


def calibrate(output: dict[str, Any], decision: PolicyDecision) -> None:
    """Confidence may never exceed what the evidence quality allows."""
    cap = 1.0 if not decision.conflicts else CONFLICT_CONFIDENCE_CAP
    confidence = min(output["assessment"]["confidence"], cap)
    output["assessment"]["confidence"] = confidence
    for claim in output.get("claim_assessments", []):
        claim["confidence"] = min(claim["confidence"], confidence)


def verify(state: CaseState, output: dict[str, Any], contracts: Contracts) -> list[str]:
    """Return violated invariant codes (empty list = approved)."""
    problems: list[str] = []
    try:
        contracts.validate_output(output, f"outputs/{state.case_id}.json")
    except ContractError:
        problems.append("schema")
    if output.get("case_id") != state.case_id:
        problems.append("case_id")

    ledger_refs = set(state.ledger.refs)
    cited = set(output.get("evidence_refs", []))
    for claim in output.get("claim_assessments", []):
        cited |= set(claim.get("evidence_refs", []))
    if not cited <= ledger_refs:
        problems.append("evidence_ownership")
    issue = output["assessment"]["primary_issue"]
    if issue != "insufficient_evidence" and not output.get("evidence_refs"):
        problems.append("conclusion_without_evidence")

    finance = output["financial_resolution"]
    lines_total = round(sum(line["amount_brl"] for line in finance["refund_lines"]), 2)
    refund = finance["recommended_refund_brl"]
    if abs(lines_total - refund) > CENT:
        problems.append("refund_lines_total")
    paid = extract_facts(state).captured_total
    if refund > paid + CENT:
        problems.append("refund_exceeds_captured")
    status = output["assessment"]["case_status"]
    if status != "action_required" and refund > 0:
        problems.append("refund_without_action_required")
    if status == "no_action" and any("refund" in a for a in output["resolution_actions"]):
        problems.append("refund_action_on_no_action")

    observed = {value for item in state.evidence for _, value in iter_id_fields(item.data)}
    entities = output["affected_entities"]
    if any(v not in observed for ids in entities.values() for v in ids):
        problems.append("entity_scope")
    for party in output["root_cause_analysis"]["responsible_parties"]:
        if party["party_type"] == "seller" and party["party_id"] not in (
            None,
            *entities["seller_ids"],
        ):
            problems.append("seller_not_in_scope")
        if party["party_type"] != "seller" and party["party_id"] is not None:
            problems.append("party_id_on_non_seller")

    if not 0 <= output["assessment"]["confidence"] <= 1:
        problems.append("confidence_bounds")
    return problems
