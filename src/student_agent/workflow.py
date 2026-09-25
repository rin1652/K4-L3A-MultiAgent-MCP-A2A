from __future__ import annotations

from dataclasses import dataclass, field
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


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Implement the L3A coordinator and specialist-agent workflow here.

    The starter kit intentionally does not generate a fallback answer: submitting an
    invented answer or evidence reference would violate the competition contract.
    """
    del case, gateway, trace
    raise NotImplementedError("Implement the L3A multi-agent workflow in solve_case()")
