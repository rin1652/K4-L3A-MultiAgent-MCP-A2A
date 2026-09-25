from __future__ import annotations

from typing import Any

from .agents.coordinator import TransientCaseError, run_case
from .mcp_gateway import EvidenceGateway
from .state import AGENT_ROLES, EVIDENCE_REF_PATTERN, EvidenceLedger, Handoff
from .trace import TraceWriter

__all__ = [
    "AGENT_ROLES",
    "EVIDENCE_REF_PATTERN",
    "EvidenceLedger",
    "Handoff",
    "TransientCaseError",
    "solve_case",
]


async def solve_case(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    *,
    allow_partial: bool = True,
) -> dict[str, Any]:
    """Coordinator -> specialists -> policy -> verifier; returns a verified l3a-output-v2.

    No fallback answer is invented: missing evidence yields ``insufficient_evidence``.
    With ``allow_partial=False`` a case whose evidence was lost to network errors raises
    ``TransientCaseError`` so the caller can reconnect and re-run it.
    """
    output, _ = await run_case(case, gateway, trace, allow_partial=allow_partial)
    return output
