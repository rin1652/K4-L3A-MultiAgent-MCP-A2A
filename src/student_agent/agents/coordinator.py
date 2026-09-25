"""Coordinator: fan out to specialists, one bounded follow-up round, hand off to policy."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from ..state import Actor, CaseState, CrossCaseGuard, Handoff, KnownIds
from ..trace import TraceWriter
from .specialists import SPECIALIST_TYPES, Specialist
from .toolbox import CaseToolbox, Gateway, RetryPolicy

# One guard per process: an evidence_ref may never serve two cases in the same run.
PROCESS_GUARD = CrossCaseGuard()
MAX_TRACE_REFS = 20


def _assign(trace: TraceWriter, agent: Specialist, round_no: int, decision: str) -> None:
    trace.emit(
        case_id=agent.case_id,
        event_type="task_assigned",
        actor=Actor.COORDINATOR.value,
        target=agent.actor.value,
        decision_code=decision,
        attributes={"round": round_no, "tools": ",".join(agent.tool_names)},
    )


async def collect_evidence(
    case: Mapping[str, Any],
    gateway: Gateway,
    trace: TraceWriter,
    *,
    retry: RetryPolicy | None = None,
    guard: CrossCaseGuard | None = None,
) -> CaseState:
    """Run coordinator + specialists for one case and return the gathered evidence state."""
    state = CaseState.start(case)
    toolbox = CaseToolbox(
        gateway,
        state.case_id,
        await gateway.describe_tools(),
        state.ledger,
        guard or PROCESS_GUARD,
        retry,
    )
    seed = KnownIds.from_case(case)
    agents = [kind(toolbox.scoped(kind.actor), trace, case) for kind in SPECIALIST_TYPES]

    for agent in agents:
        _assign(trace, agent, 1, "collect_evidence")
    await asyncio.gather(*(agent.run(seed) for agent in agents))

    shared = seed.copy()
    for agent in agents:
        shared.merge(agent.known)
    follow_ups = [agent for agent in agents if agent.resolvable(shared)]
    for agent in follow_ups:
        _assign(trace, agent, 2, "follow_up")
    await asyncio.gather(*(agent.follow_up(shared) for agent in follow_ups))

    for agent in agents:
        agent.finish()
        result = agent.result()
        state.add_result(result)
        handoff = Handoff(
            case_id=state.case_id,
            source=agent.actor.value,
            target=Actor.POLICY.value,
            task="decide_case",
            entity_ids=tuple(
                dict.fromkeys(v for ids in result.entities.to_output().values() for v in ids)
            ),
            evidence_refs=result.evidence_refs,
            payload=result,
        )
        state.handoffs.append(handoff)
        trace.emit(
            case_id=state.case_id,
            event_type="handoff",
            actor=agent.actor.value,
            target=Actor.POLICY.value,
            decision_code="evidence_complete" if result.complete else "evidence_partial",
            evidence_refs=list(result.evidence_refs[:MAX_TRACE_REFS]) or None,
            attributes={
                "evidence_count": len(result.evidence),
                "missing_count": len(result.missing),
                "conflict_count": len(result.conflicts),
            },
        )
    return state
