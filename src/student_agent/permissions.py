"""Which actor may call which MCP tool, and which evidence domains it may consume.

Tool names are the ones the competition brief lists for discovery; a tool is only ever
called if ``gateway.describe_tools()`` actually returned it. Update this table from
docs/mcp-tools.md once discovery succeeds -- it is the single place permissions live.
"""

from __future__ import annotations

from collections.abc import Mapping

from .state import Actor

TOOL_GRANTS: Mapping[Actor, Mapping[str, str]] = {
    # actor -> {tool_name: primary evidence domain}
    Actor.COORDINATOR: {},
    Actor.ORDER_ITEM: {"get_order": "order", "get_seller": "seller"},
    Actor.PAYMENT: {"get_payment": "payment"},
    Actor.SHIPMENT: {"get_shipment": "shipment"},
    Actor.POLICY: {"get_policy": "policy"},
    Actor.VERIFIER: {},
}

# Envelope domains each actor may consume (order data may embed item/customer/product rows,
# payment data may embed refund rows).
DOMAIN_GRANTS: Mapping[Actor, frozenset[str]] = {
    Actor.COORDINATOR: frozenset(),
    Actor.ORDER_ITEM: frozenset({"order", "item", "seller", "product", "customer"}),
    Actor.PAYMENT: frozenset({"payment", "refund"}),
    Actor.SHIPMENT: frozenset({"shipment"}),
    Actor.POLICY: frozenset({"policy"}),
    Actor.VERIFIER: frozenset(),
}


class ToolPermissionError(PermissionError):
    pass


def allowed_tools(actor: Actor) -> frozenset[str]:
    return frozenset(TOOL_GRANTS[actor])


def check_tool(actor: Actor, tool_name: str) -> None:
    if tool_name not in TOOL_GRANTS[actor]:
        raise ToolPermissionError(f"{actor.value} is not allowed to call {tool_name}")


def owner_of(tool_name: str) -> tuple[str | None, str | None]:
    for actor, grants in TOOL_GRANTS.items():
        if tool_name in grants:
            return actor.value, grants[tool_name]
    return None, None
