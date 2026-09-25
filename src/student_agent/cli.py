from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


def _iter_exceptions(exc: BaseException) -> list[BaseException]:
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    ordered: list[BaseException] = []
    while stack:
        current = stack.pop()
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        ordered.append(current)
        if isinstance(current, BaseExceptionGroup):
            stack.extend(current.exceptions)
        for linked in (getattr(current, "__cause__", None), getattr(current, "__context__", None)):
            if isinstance(linked, BaseException):
                stack.append(linked)
    return ordered


_NETWORK_MARKERS = (
    "connecterror",
    "connecttimeout",
    "readtimeout",
    "writetimeout",
    "pooltimeout",
    "remoteprotocolerror",
    "getaddrinfo",
    "temporarily unavailable",
    "connection reset",
    "server disconnected",
    "brokenresource",
    "closedresource",
    "ssl",
    "server returned an error response",  # MCPError at session.initialize()
    "mcperror",
)
_TOOL_FAIL_MARKER = "mcp tool"  # RuntimeError("MCP tool X failed: ...")


def _is_transient_network_error(exc: BaseException) -> bool:
    """Return True only when the root cause is a genuine network/transport error.

    Tool-level failures (RuntimeError from gateway.call) are NOT transient —
    they indicate a server-side issue with a specific tool call, not the
    connection itself.
    """
    leafs = _iter_exceptions(exc)
    has_network = False
    for item in leafs:
        name = type(item).__name__.lower()
        text = str(item).lower()
        # Tool-level failure ("MCP tool X failed: ...") is not a network error
        if _TOOL_FAIL_MARKER in text and " failed" in text:
            return False
        if any(marker in name or marker in text for marker in _NETWORK_MARKERS):
            has_network = True
    return has_network


async def _run(root: Path) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    for stale in output_root.glob("*.json"):
        stale.unlink()
    trace_path.unlink(missing_ok=True)
    trace = TraceWriter(trace_path, contracts)

    case_ids = list(case_set.case_ids)
    index = 0
    reconnect_attempts = 0
    batch_size = 25

    while index < len(case_ids):
        batch_end = min(index + batch_size, len(case_ids))
        try:
            async with connect_gateway(
                settings.mcp_endpoint, settings.team_api_key, contracts
            ) as gateway:
                discovered_tools = await gateway.list_tools()
                if not discovered_tools:
                    raise RuntimeError("MCP Gateway returned no tools")
                while index < batch_end:
                    case_id = case_ids[index]
                    case = case_set.cases[case_id]
                    trace.emit(
                        case_id=case_id, event_type="case_received", actor="coordinator"
                    )
                    output = await solve_case(case, gateway, trace)
                    contracts.validate_output(output, f"outputs/{case_id}.json")
                    if output.get("case_id") != case_id:
                        raise ValueError(
                            f"solver returned a mismatched case_id for {case_id}"
                        )
                    if not output.get("evidence_refs"):
                        raise RuntimeError(
                            f"MCP returned no evidence for {case_id}; "
                            "server may be blocking this run — wait and retry later"
                        )
                    target = output_root / f"{case_id}.json"
                    temporary = target.with_suffix(".json.tmp")
                    temporary.write_text(
                        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    temporary.replace(target)
                    trace.emit(
                        case_id=case_id,
                        event_type="case_finalized",
                        actor="coordinator",
                    )
                    index += 1
                    reconnect_attempts = 0
                    print(f"OK {case_id} ({index}/{len(case_ids)})", flush=True)
                await asyncio.sleep(0.5)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if not _is_transient_network_error(exc):
                raise
            reconnect_attempts += 1
            case_id = case_ids[min(index, len(case_ids) - 1)]
            if reconnect_attempts > 8:
                raise RuntimeError(
                    f"MCP network failed repeatedly at {case_id}"
                ) from exc
            wait_s = min(5 * reconnect_attempts, 40)
            print(
                f"WARN network drop near {case_id}; reconnect "
                f"{reconnect_attempts}/8 after {wait_s}s ({type(exc).__name__})",
                flush=True,
            )
            await asyncio.sleep(wait_s)



def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3A student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    commands.add_parser("run", help="run the implemented workflow for all cases")
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / "
                f"{len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(_run(root))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError, BaseExceptionGroup) as exc:
        import traceback as _tb
        _tb.print_exc(file=sys.stderr)
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
