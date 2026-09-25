"""Batch run (``day09 run``) that survives a flaky MCP connection.

- Each case writes its trace to ``traces/.pending/<case_id>.jsonl`` first; only a case that
  finished is appended to ``traces/trace.jsonl`` (after its output file), so a crash never
  leaves half a case in the trace and ``--resume`` can continue safely.
- When the MCP session drops, the runner reconnects and re-runs the interrupted case.
  Earlier attempts use ``allow_partial=False``; the last attempt accepts missing evidence
  (reported as such, never guessed).
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cases import load_case_set
from .config import Settings
from .contracts import ContractError, Contracts
from .mcp_gateway import (
    EvidenceGateway,
    GatewayConnectionError,
    connect_gateway,
    describe_error,
    is_transport_error,
)
from .trace import TraceWriter
from .workflow import TransientCaseError, solve_case

MAX_CASE_ATTEMPTS = 3
MAX_SESSION_FAILURES = 30


@dataclass
class RunReport:
    total: int
    skipped: int = 0
    completed: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    issues: Counter[str] = field(default_factory=Counter)
    reconnects: int = 0


def _finalized_cases(trace_path: Path) -> set[str]:
    if not trace_path.exists():
        return set()
    done = set()
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            event = json.loads(line)
            if event.get("event_type") == "case_finalized":
                done.add(event["case_id"])
    return done


def _valid_output(path: Path, case_id: str, contracts: Contracts) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        contracts.validate_output(value, path.name)
    except (OSError, ValueError, ContractError):
        return False
    return value.get("case_id") == case_id


async def _run_one(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    contracts: Contracts,
    root: Path,
    *,
    allow_partial: bool,
) -> dict[str, Any]:
    case_id = case["case_id"]
    pending = root / "traces" / ".pending" / f"{case_id}.jsonl"
    pending.unlink(missing_ok=True)
    trace = TraceWriter(pending, contracts)
    try:
        trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
        output = await solve_case(case, gateway, trace, allow_partial=allow_partial)
        contracts.validate_output(output, f"outputs/{case_id}.json")
        if output.get("case_id") != case_id:
            raise ValueError(f"solver returned a mismatched case_id for {case_id}")
        target = root / "outputs" / f"{case_id}.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(target)
        trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
        with (root / "traces" / "trace.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(pending.read_text(encoding="utf-8"))
        return output
    finally:
        pending.unlink(missing_ok=True)


async def run_batch(
    root: Path, *, resume: bool = False, log: Callable[[str], None] = print
) -> RunReport:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    (root / "traces" / ".pending").mkdir(parents=True, exist_ok=True)

    done: set[str] = set()
    if resume:
        finalized = _finalized_cases(trace_path)
        done = {
            case_id
            for case_id in case_set.case_ids
            if case_id in finalized
            and _valid_output(output_root / f"{case_id}.json", case_id, contracts)
        }
    else:
        for stale in output_root.glob("*.json"):
            stale.unlink()
        trace_path.unlink(missing_ok=True)
    report = RunReport(total=len(case_set.case_ids), skipped=len(done))
    pending = [case_id for case_id in case_set.case_ids if case_id not in done]
    attempts: Counter[str] = Counter()
    session_failures = 0

    while pending:
        try:
            async with connect_gateway(
                settings.mcp_endpoint, settings.team_api_key, contracts
            ) as gateway:
                if not await gateway.list_tools():
                    raise RuntimeError("MCP Gateway returned no tools")
                while pending:
                    case_id = pending[0]
                    attempts[case_id] += 1
                    last_try = attempts[case_id] >= MAX_CASE_ATTEMPTS
                    try:
                        output = await _run_one(
                            case_set.cases[case_id],
                            gateway,
                            contracts,
                            root,
                            allow_partial=last_try,
                        )
                    except TransientCaseError:
                        log(f"  {case_id}: evidence lost to network, reconnecting")
                        break
                    pending.pop(0)
                    issue = output["assessment"]["primary_issue"]
                    report.completed.append(case_id)
                    report.issues[issue] += 1
                    done_count = report.skipped + len(report.completed)
                    log(
                        f"[{done_count}/{report.total}] {case_id}: {issue} "
                        f"refund={output['financial_resolution']['recommended_refund_brl']} "
                        f"confidence={output['assessment']['confidence']}"
                    )
        except GatewayConnectionError as exc:
            if not exc.transient:
                raise
            session_failures += 1
            log(f"  {exc}; waiting before the next attempt")
            await asyncio.sleep(10)
        except BaseException as exc:
            if not is_transport_error(exc):
                raise
            session_failures += 1
            log(f"  session dropped ({describe_error(exc)}); reconnecting")
        report.reconnects = session_failures
        if pending and attempts[pending[0]] > MAX_CASE_ATTEMPTS + 1:
            case_id = pending.pop(0)
            report.failed[case_id] = "network failures on every attempt"
            log(f"  {case_id}: giving up for this run (use --resume later)")
        if session_failures > MAX_SESSION_FAILURES:
            for case_id in pending:
                report.failed[case_id] = "not attempted: too many session failures"
            break
    return report
