from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import anyio
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED, REQUEST_TIMEOUT

from .contracts import Contracts

TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    TimeoutError,
    ConnectionError,
    httpx2.TransportError,
    anyio.ClosedResourceError,
    anyio.BrokenResourceError,
)
TRANSIENT_MCP_CODES = frozenset({CONNECTION_CLOSED, REQUEST_TIMEOUT})
CONNECT_ATTEMPTS = 3
CONNECT_BACKOFF_SECONDS = 2.0


def _tool_input_schema(tool: Any) -> dict[str, Any]:
    # mcp>=2 exposes ``input_schema``; 1.x used ``inputSchema``.
    schema = getattr(tool, "input_schema", None)
    if schema is None:
        schema = getattr(tool, "inputSchema", None)
    return schema or {}


class GatewayConnectionError(RuntimeError):
    """The MCP session could not be opened; the message never contains credentials."""

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


def leaf_errors(exc: BaseException) -> list[BaseException]:
    """Flatten (nested) exception groups raised by anyio task groups."""
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for inner in exc.exceptions for leaf in leaf_errors(inner)]
    return [exc]


def is_transport_error(exc: BaseException) -> bool:
    """Network-level failure (timeout, refused/dropped connection); retrying may help."""
    leaves = leaf_errors(exc)
    return bool(leaves) and all(
        isinstance(leaf, TRANSIENT_ERRORS)
        or (isinstance(leaf, MCPError) and leaf.code in TRANSIENT_MCP_CODES)
        for leaf in leaves
    )


def describe_error(exc: BaseException) -> str:
    return "; ".join(
        f"{type(leaf).__name__}: {' '.join(str(leaf).split()) or '-'}"[:200]
        for leaf in leaf_errors(exc)
    )


class EvidenceGateway:
    def __init__(self, session: ClientSession, contracts: Contracts) -> None:
        self._session = session
        self._contracts = contracts
        self._tool_specs: dict[str, dict[str, Any]] | None = None

    async def list_tools(self) -> list[str]:
        return sorted(await self.describe_tools())

    async def describe_tools(self) -> dict[str, dict[str, Any]]:
        """Discovered tools as ``{name: {"description": str, "input_schema": dict}}`` (cached)."""
        if self._tool_specs is None:
            response = await self._session.list_tools()
            self._tool_specs = {
                tool.name: {
                    "description": tool.description or "",
                    "input_schema": dict(_tool_input_schema(tool)),
                }
                for tool in response.tools
            }
        return self._tool_specs

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        payload = {"case_id": case_id, **arguments}
        result = await self._session.call_tool(tool_name, arguments=payload)
        # mcp>=2 names the flag ``is_error``; 1.x used ``isError``.
        if getattr(result, "is_error", None) or getattr(result, "isError", None):
            message = " ".join(
                block.text for block in result.content if getattr(block, "text", None)
            )
            raise RuntimeError(f"MCP tool {tool_name} failed: {message or 'unknown error'}")
        evidence = getattr(result, "structuredContent", None)
        if evidence is None:
            evidence = getattr(result, "structured_content", None)
        if evidence is None:
            text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
            if len(text_blocks) != 1:
                raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
            evidence = json.loads(text_blocks[0])
        self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        return evidence


@asynccontextmanager
async def connect_gateway(
    endpoint: str,
    team_api_key: str,
    contracts: Contracts,
    *,
    attempts: int = CONNECT_ATTEMPTS,
    backoff: float = CONNECT_BACKOFF_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AsyncIterator[EvidenceGateway]:
    """Open an authenticated MCP session, retrying only network failures while opening.

    Errors raised after the session is handed out are not retried here (the caller's
    work may already have run); per-call retry lives in ``agents.toolbox``.
    """
    for attempt in range(1, attempts + 1):
        stack = AsyncExitStack()
        try:
            gateway = await stack.enter_async_context(
                _open_session(endpoint, team_api_key, contracts)
            )
        except Exception as exc:
            await stack.aclose()
            if not is_transport_error(exc):
                raise GatewayConnectionError(
                    f"MCP gateway rejected the session (not retried): {describe_error(exc)}",
                    transient=False,
                ) from exc
            if attempt == attempts:
                raise GatewayConnectionError(
                    f"cannot reach MCP gateway after {attempts} attempts: {describe_error(exc)}",
                    transient=True,
                ) from exc
            await sleep(backoff * attempt)
            continue
        async with stack:
            yield gateway
        return


@asynccontextmanager
async def _open_session(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    headers = {"Authorization": f"Bearer {team_api_key}"}
    timeout = httpx2.Timeout(300.0, connect=30.0, write=30.0, pool=30.0)
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        streamable_http_client(endpoint, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield EvidenceGateway(session, contracts)
