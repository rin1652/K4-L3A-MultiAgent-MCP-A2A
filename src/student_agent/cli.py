from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway, describe_error, is_transport_error
from .permissions import owner_of
from .probe import probe_cases, summarize
from .runner import run_batch
from .submission import package_submission, validate_artifacts
from .tools_doc import render_tools_markdown


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path, doc: str | None = None) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)
        if doc:
            target = root / doc
            target.parent.mkdir(parents=True, exist_ok=True)
            markdown = render_tools_markdown(await gateway.describe_tools(), owner_of)
            target.write_text(markdown, encoding="utf-8")
            print(f"OK: {target}")


async def _run(root: Path, resume: bool) -> None:
    report = await run_batch(root, resume=resume)
    print(
        f"done: {len(report.completed)} solved, {report.skipped} kept from earlier run, "
        f"{len(report.failed)} failed, {report.reconnects} reconnects"
    )
    for issue, count in sorted(report.issues.items()):
        print(f"  {issue}: {count}")
    if report.failed:
        for case_id, reason in report.failed.items():
            print(f"  FAILED {case_id}: {reason}")
        raise RuntimeError("some cases did not finish; re-run with `day09 run --resume`")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3A student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    tools = commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    tools.add_argument("--doc", help="also write a Markdown catalogue, e.g. docs/mcp-tools.md")
    probe = commands.add_parser("probe", help="collect evidence for cases and dump it to debug/")
    probe.add_argument("case_ids", nargs="+", metavar="CASE_ID")
    probe.add_argument("--input-dir", default="inputs", help="folder with <case_id>.json")
    probe.add_argument("--out", default="debug", help="dump folder (git-ignored)")
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument(
        "--resume", action="store_true", help="keep finished cases and continue with the rest"
    )
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
                f"OK: {case_set.variant_id} / {case_set.version} / {len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root, args.doc))
        elif args.command == "probe":
            states = asyncio.run(
                probe_cases(root, args.case_ids, root / args.input_dir, root / args.out)
            )
            for state, output in states:
                print(summarize(state, output))
            print(f"OK: {root / args.out}")
        elif args.command == "run":
            asyncio.run(_run(root, args.resume))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except BaseExceptionGroup as group:
        if not is_transport_error(group):
            raise
        print(f"ERROR: MCP connection lost: {describe_error(group)}", file=sys.stderr)
        raise SystemExit(1) from group


if __name__ == "__main__":
    main()
