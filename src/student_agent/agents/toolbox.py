"""Case-scoped MCP access: permission check, fixed case_id, bounded retry, per-case dedupe."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import anyio
import httpx2
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED, REQUEST_TIMEOUT

from ..contracts import ContractError
from ..permissions import DOMAIN_GRANTS, check_tool
from ..state import Actor, CrossCaseGuard, EvidenceItem, EvidenceLedger, MissingReason


class Gateway(Protocol):
    async def describe_tools(self) -> dict[str, dict[str, Any]]: ...

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]: ...


TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    TimeoutError,
    ConnectionError,
    httpx2.TransportError,
    anyio.ClosedResourceError,
    anyio.BrokenResourceError,
)
CallKey = tuple[str, tuple[tuple[str, str], ...]]
TRANSIENT_MCP_CODES = frozenset({CONNECTION_CLOSED, REQUEST_TIMEOUT})
# Word-bounded so hex identifiers such as "a404b" never match a status code.
_NOT_FOUND = re.compile(r"not[ _]found|\bno such\b|\b404\b")
_FORBIDDEN = re.compile(r"forbidden|not allowed|out of scope|unauthori[sz]ed|\b40[13]\b")


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 2
    base_delay: float = 0.5
    call_timeout: float = 60.0
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


class ToolCallFailure(Exception):
    def __init__(self, reason: MissingReason, tool_name: str, detail: str) -> None:
        super().__init__(f"{tool_name}: {reason.value}: {detail}")
        self.reason = reason
        self.tool_name = tool_name
        self.detail = detail


def _short(exc: BaseException) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:160]


def classify_error(exc: BaseException) -> MissingReason:
    if isinstance(exc, ContractError):
        return MissingReason.INVALID_ENVELOPE
    text = str(exc).lower()
    if _NOT_FOUND.search(text):
        return MissingReason.NOT_FOUND
    if _FORBIDDEN.search(text):
        return MissingReason.FORBIDDEN
    return MissingReason.TOOL_ERROR


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, TRANSIENT_ERRORS):
        return True
    return (
        isinstance(exc, MCPError)
        and exc.code in TRANSIENT_MCP_CODES
        and classify_error(exc) is MissingReason.TOOL_ERROR
    )


class CaseToolbox:
    """All MCP traffic of one case goes through here; nothing is shared across cases."""

    def __init__(
        self,
        gateway: Gateway,
        case_id: str,
        tool_specs: Mapping[str, Mapping[str, Any]],
        ledger: EvidenceLedger,
        guard: CrossCaseGuard,
        retry: RetryPolicy | None = None,
    ) -> None:
        if ledger.case_id != case_id:
            raise ValueError("ledger belongs to another case")
        self._gateway = gateway
        self.case_id = case_id
        self._specs = tool_specs
        self.ledger = ledger
        self._guard = guard
        self._retry = retry or RetryPolicy()
        self._cache: dict[CallKey, asyncio.Task[dict[str, Any]]] = {}

    def scoped(self, actor: Actor) -> ScopedGateway:
        return ScopedGateway(self, actor)

    def required_params(self, tool_name: str) -> tuple[str, ...] | None:
        """``None`` if discovery did not return the tool; ``case_id`` is always excluded."""
        spec = self._specs.get(tool_name)
        if spec is None:
            return None
        schema = spec.get("input_schema") or {}
        return tuple(param for param in schema.get("required", ()) if param != "case_id")

    async def fetch(self, tool_name: str, arguments: Mapping[str, str]) -> dict[str, Any]:
        key = (tool_name, tuple(sorted(arguments.items())))
        task = self._cache.get(key)
        if task is None:
            task = asyncio.ensure_future(self._call_with_retry(tool_name, dict(arguments)))
            self._cache[key] = task
        return await task

    async def _call_with_retry(self, tool_name: str, arguments: dict[str, str]) -> dict[str, Any]:
        policy = self._retry
        for attempt in range(policy.max_retries + 1):
            try:
                return await asyncio.wait_for(
                    self._gateway.call(tool_name, case_id=self.case_id, **arguments),
                    policy.call_timeout,
                )
            except Exception as exc:
                if not is_transient(exc):
                    raise ToolCallFailure(classify_error(exc), tool_name, _short(exc)) from exc
                if attempt == policy.max_retries:
                    raise ToolCallFailure(
                        MissingReason.TRANSIENT_EXHAUSTED, tool_name, _short(exc)
                    ) from exc
                await policy.sleep(policy.base_delay * 2**attempt)
        raise AssertionError("unreachable")

    def accept(self, actor: Actor, envelope: Mapping[str, Any]) -> None:
        if envelope["domain"] not in DOMAIN_GRANTS[actor]:
            raise ToolCallFailure(
                MissingReason.DOMAIN_MISMATCH,
                "",
                f"{actor.value} may not consume domain {envelope['domain']}",
            )
        self.ledger.record(dict(envelope))
        self._guard.claim(self.case_id, envelope["evidence_ref"])


class ScopedGateway:
    """What a specialist sees: only its own tools, only this case."""

    def __init__(self, toolbox: CaseToolbox, actor: Actor) -> None:
        self._toolbox = toolbox
        self.actor = actor

    @property
    def case_id(self) -> str:
        return self._toolbox.case_id

    def required_params(self, tool_name: str) -> tuple[str, ...] | None:
        check_tool(self.actor, tool_name)
        return self._toolbox.required_params(tool_name)

    async def call(self, tool_name: str, **arguments: str) -> EvidenceItem:
        check_tool(self.actor, tool_name)
        if "case_id" in arguments:
            raise ValueError("case_id is fixed by the case toolbox")
        envelope = await self._toolbox.fetch(tool_name, arguments)
        try:
            self._toolbox.accept(self.actor, envelope)
        except ToolCallFailure as failure:
            raise ToolCallFailure(failure.reason, tool_name, failure.detail) from None
        return EvidenceItem.from_envelope(
            actor=self.actor.value, tool_name=tool_name, arguments=arguments, envelope=envelope
        )
