from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
import pytest

from student_agent import mcp_gateway
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import GatewayConnectionError, connect_gateway, is_transport_error

CONTRACTS = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
KEY = "sk-team-" + "x" * 24
ENDPOINT = "https://gw.test/mcp"


def _fake_open(outcomes: list[BaseException | None], opened: list[int]) -> Any:
    @asynccontextmanager
    async def fake(_endpoint: str, _key: str, _contracts: Contracts) -> AsyncIterator[str]:
        opened.append(1)
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome
        yield "gateway"

    return fake


def _connect_group() -> BaseExceptionGroup:
    return ExceptionGroup("tg", [httpx2.ConnectError("All connection attempts failed")])


def _open(
    outcomes: list[BaseException | None], monkeypatch: pytest.MonkeyPatch
) -> tuple[str, int, list[float]]:
    opened: list[int] = []
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(mcp_gateway, "_open_session", _fake_open(outcomes, opened))

    async def main() -> str:
        async with connect_gateway(ENDPOINT, KEY, CONTRACTS, sleep=sleep) as gateway:
            return gateway

    return asyncio.run(main()), len(opened), delays


def test_transport_errors_while_opening_are_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway, opened, delays = _open(
        [_connect_group(), httpx2.ConnectTimeout("timed out"), None], monkeypatch
    )
    assert gateway == "gateway"
    assert opened == 3
    assert delays == [2.0, 4.0]


def test_gives_up_after_three_attempts_with_a_clean_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(GatewayConnectionError, match="after 3 attempts: ConnectError") as info:
        _open([_connect_group() for _ in range(3)], monkeypatch)
    assert "sk-team-" not in str(info.value)


def test_auth_errors_are_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx2.Request("POST", ENDPOINT)
    denied = httpx2.HTTPStatusError(
        "Client error '401 Unauthorized'", request=request, response=httpx2.Response(401)
    )
    with pytest.raises(GatewayConnectionError, match="not retried.*401"):
        _open([denied, None], monkeypatch)


def test_errors_inside_the_session_are_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[int] = []
    monkeypatch.setattr(mcp_gateway, "_open_session", _fake_open([None, None], opened))

    async def main() -> None:
        async with connect_gateway(ENDPOINT, KEY, CONTRACTS):
            raise httpx2.ConnectError("mid-run")

    with pytest.raises(httpx2.ConnectError):
        asyncio.run(main())
    assert opened == [1]


def test_transport_classification_flattens_groups() -> None:
    assert is_transport_error(_connect_group())
    assert not is_transport_error(ExceptionGroup("tg", [_connect_group(), ValueError("x")]))
