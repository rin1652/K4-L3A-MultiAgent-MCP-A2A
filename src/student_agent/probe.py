"""``day09 probe``: run the full pipeline for a few cases; dump evidence, output and trace."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .agents.coordinator import run_case
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


def summarize(state: CaseState, output: dict[str, Any]) -> str:
    domains = {domain: len(items) for domain, items in state.evidence_by_domain.items()}
    missing = sorted({f"{entry.tool_name}:{entry.reason.value}" for entry in state.missing})
    assessment = output["assessment"]
    return (
        f"{state.case_id}: {assessment['primary_issue']} ({assessment['case_status']}, "
        f"refund={output['financial_resolution']['recommended_refund_brl']}, "
        f"confidence={assessment['confidence']}); evidence {domains}; "
        f"missing={missing or '-'}"
    )


async def probe_cases(
    root: Path, case_ids: list[str], input_dir: Path, out_dir: Path
) -> list[tuple[CaseState, dict[str, Any]]]:
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
            output, state = await run_case(case, gateway, trace)
            contracts.validate_output(output, f"{case_id} output")
            trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
            bundle = {**state.to_debug_dict(), "output": output, "trace_file": trace_path.name}
            (out_dir / f"{case_id}.evidence.json").write_text(
                json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            states.append((state, output))
    return states
