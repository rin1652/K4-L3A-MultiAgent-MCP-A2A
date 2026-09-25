"""``day09 probe``: run coordinator + specialists for a few cases and dump the evidence bundle."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .agents import collect_evidence
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .state import CaseState
from .trace import TraceWriter


def _load_case(input_dir: Path, case_id: str) -> dict[str, Any]:
    path = input_dir / f"{case_id}.json"
    case = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(case, dict) or case.get("case_id") != case_id:
        raise ValueError(f"{path}: case_id does not match {case_id}")
    return case


def summarize(state: CaseState) -> str:
    domains = {domain: len(items) for domain, items in state.evidence_by_domain.items()}
    missing = sorted({f"{entry.tool_name}:{entry.reason.value}" for entry in state.missing})
    return (
        f"{state.case_id}: {len(state.evidence_refs)} evidence {domains}; "
        f"missing={missing or '-'}; conflicts={len(state.conflicts)}"
    )


async def probe_cases(
    root: Path, case_ids: list[str], input_dir: Path, out_dir: Path
) -> list[CaseState]:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    cases = [_load_case(input_dir, case_id) for case_id in case_ids]
    out_dir.mkdir(parents=True, exist_ok=True)
    states = []
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for case in cases:
            case_id = case["case_id"]
            trace_path = out_dir / f"{case_id}.trace.jsonl"
            trace_path.unlink(missing_ok=True)
            trace = TraceWriter(trace_path, contracts)
            trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
            state = await collect_evidence(case, gateway, trace)
            bundle = {**state.to_debug_dict(), "trace_file": trace_path.name}
            (out_dir / f"{case_id}.evidence.json").write_text(
                json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            states.append(state)
    return states
