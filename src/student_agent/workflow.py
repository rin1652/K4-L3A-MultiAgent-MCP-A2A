from __future__ import annotations

from typing import Any

from .mcp_gateway import EvidenceGateway
from .state import AGENT_ROLES, EVIDENCE_REF_PATTERN, EvidenceLedger, Handoff
from .trace import TraceWriter

__all__ = ["AGENT_ROLES", "EVIDENCE_REF_PATTERN", "EvidenceLedger", "Handoff", "solve_case"]


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Implement the L3A coordinator and specialist-agent workflow here.

    The starter kit intentionally does not generate a fallback answer: submitting an
    invented answer or evidence reference would violate the competition contract.
    """
    del case, gateway, trace
    raise NotImplementedError("Implement the L3A multi-agent workflow in solve_case()")
