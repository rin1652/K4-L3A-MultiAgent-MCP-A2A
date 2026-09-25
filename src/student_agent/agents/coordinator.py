"""Coordinator: fan out to specialists, one bounded follow-up round, then
policy -> verifier (at most one revision)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from ..state import Actor, CaseState, CrossCaseGuard, Handoff, KnownIds, MissingReason
from ..trace import TraceWriter
from .policy import PolicyAgent, PolicyDecision, build_output, decide
from .specialists import SPECIALIST_TYPES, Specialist
from .toolbox import CaseToolbox, Gateway, RetryPolicy
from .verifier import calibrate, verify

# One guard per process: an evidence_ref may never serve two cases in the same run.
PROCESS_GUARD = CrossCaseGuard()
MAX_TRACE_REFS = 20


class TransientCaseError(RuntimeError):
    """Evidence is missing only because the network failed; the case should be re-run."""


def _assign(trace: TraceWriter, agent: Specialist, round_no: int, decision: str) -> None:
    trace.emit(
        case_id=agent.case_id,
        event_type="task_assigned",
        actor=Actor.COORDINATOR.value,
        target=agent.actor.value,
        decision_code=decision,
        attributes={"round": round_no, "tools": ",".join(agent.tool_names)},
    )


async def _collect(
    case: Mapping[str, Any],
    gateway: Gateway,
    trace: TraceWriter,
    retry: RetryPolicy | None,
    guard: CrossCaseGuard | None,
) -> tuple[CaseState, CaseToolbox]:
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
        state.handoffs.append(
            Handoff(
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
        )
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
    return state, toolbox


async def collect_evidence(
    case: Mapping[str, Any],
    gateway: Gateway,
    trace: TraceWriter,
    *,
    retry: RetryPolicy | None = None,
    guard: CrossCaseGuard | None = None,
) -> CaseState:
    """Run coordinator + specialists for one case and return the gathered evidence state."""
    state, _ = await _collect(case, gateway, trace, retry, guard)
    return state


def _emit_decision(trace: TraceWriter, state: CaseState, decision: PolicyDecision) -> None:
    trace.emit(
        case_id=state.case_id,
        event_type="policy_decided",
        actor=Actor.POLICY.value,
        decision_code=decision.primary_issue,
        evidence_refs=decision.cited[:MAX_TRACE_REFS] or None,
        attributes={
            "case_status": decision.case_status,
            "refund_brl": decision.refund,
            "confidence": decision.confidence,
            "attempt": decision.attempt,
        },
    )
    trace.emit(
        case_id=state.case_id,
        event_type="handoff",
        actor=Actor.POLICY.value,
        target=Actor.VERIFIER.value,
        decision_code="verify_decision",
        evidence_refs=decision.cited[:MAX_TRACE_REFS] or None,
    )


async def run_case(
    case: Mapping[str, Any],
    gateway: Gateway,
    trace: TraceWriter,
    *,
    retry: RetryPolicy | None = None,
    guard: CrossCaseGuard | None = None,
    allow_partial: bool = True,
) -> tuple[dict[str, Any], CaseState]:
    """Full pipeline for one case: specialists -> policy -> verifier -> output."""
    state, toolbox = await _collect(case, gateway, trace, retry, guard)
    policy = PolicyAgent(toolbox.scoped(Actor.POLICY), trace, case)
    await policy.run(KnownIds())
    state.add_result(policy.result())
    if not allow_partial and any(
        entry.reason is MissingReason.TRANSIENT_EXHAUSTED for entry in state.missing
    ):
        raise TransientCaseError(f"{state.case_id}: evidence lost to network errors")

    decision = decide(state)
    _emit_decision(trace, state, decision)
    output = build_output(state, decision)
    calibrate(output, decision)
    problems = verify(state, output, trace.contracts)
    verdict = "approved"
    if problems:
        trace.emit(
            case_id=state.case_id,
            event_type="handoff",
            actor=Actor.VERIFIER.value,
            target=Actor.POLICY.value,
            decision_code="revise",
            attributes={"violations": ",".join(problems)[:200]},
        )
        decision = decide(state, conservative=True)
        decision.attempt = 2
        _emit_decision(trace, state, decision)
        output = build_output(state, decision)
        calibrate(output, decision)
        remaining = verify(state, output, trace.contracts)
        if remaining:
            raise ValueError(f"{state.case_id}: verifier rejected output: {remaining}")
        verdict = "revised"
    trace.emit(
        case_id=state.case_id,
        event_type="verification_completed",
        actor=Actor.VERIFIER.value,
        decision_code=verdict,
        evidence_refs=output["evidence_refs"][:MAX_TRACE_REFS] or None,
        attributes={
            "primary_issue": output["assessment"]["primary_issue"],
            "confidence": output["assessment"]["confidence"],
            "violations_first_pass": len(problems),
        },
    )
    return output, state
