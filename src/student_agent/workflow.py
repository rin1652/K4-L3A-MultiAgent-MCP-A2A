from __future__ import annotations

from typing import Any

from .agents import collect_evidence
from .mcp_gateway import EvidenceGateway
from .state import AGENT_ROLES, EVIDENCE_REF_PATTERN, CaseState, EvidenceLedger, Handoff
from .trace import TraceWriter

__all__ = ["AGENT_ROLES", "EVIDENCE_REF_PATTERN", "EvidenceLedger", "Handoff", "solve_case"]


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinator + specialists gather evidence (Pha 3); policy/verifier decide (Pha 4).

    No fallback answer is generated: an invented answer or evidence reference would
    violate the competition contract.
    """
    state = await collect_evidence(case, gateway, trace)
    return decide_and_verify(state, trace)


def decide_and_verify(state: CaseState, trace: TraceWriter) -> dict[str, Any]:
    """Policy agent + verifier: turn the evidence state into an l3a-output-v2 object."""
    raise NotImplementedError("Pha 4: policy/verifier")
