"""Render the discovered MCP tool catalogue as Markdown (``day09 mcp-tools --doc``)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


def _param_type(spec: Mapping[str, Any]) -> str:
    kind = spec.get("type")
    if isinstance(kind, list):
        return " | ".join(str(item) for item in kind)
    if kind is None and "anyOf" in spec:
        return " | ".join(str(item.get("type", "?")) for item in spec["anyOf"])
    return str(kind or "?")


def _cell(text: str) -> str:
    return " ".join(text.replace("|", "\\|").split())


def render_tools_markdown(
    tools: Mapping[str, Mapping[str, Any]],
    owner_of: Callable[[str], tuple[str | None, str | None]] = lambda _name: (None, None),
) -> str:
    """``owner_of(tool) -> (actor, domain)`` comes from the agent permission table."""
    lines = [
        "# MCP tools",
        "",
        "Sinh tự động từ tool discovery của MCP Evidence Gateway (`day09 mcp-tools --doc`).",
        "Không sửa tay: chạy lại lệnh khi gateway thay đổi.",
        "",
        "| Tool | Domain | Agent được phép gọi | Tham số bắt buộc | Mô tả |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name in sorted(tools):
        schema = tools[name].get("input_schema") or {}
        required = [param for param in schema.get("required", []) if param != "case_id"]
        actor, domain = owner_of(name)
        lines.append(
            f"| `{name}` | {domain or '—'} | {actor or '— (chưa phân quyền)'} | "
            f"{', '.join(f'`{param}`' for param in required) or '—'} | "
            f"{_cell(str(tools[name].get('description', '')))} |"
        )
    for name in sorted(tools):
        schema = tools[name].get("input_schema") or {}
        properties: Mapping[str, Any] = schema.get("properties") or {}
        required = set(schema.get("required", []))
        lines += [
            "",
            f"## `{name}`",
            "",
            _cell(str(tools[name].get("description", ""))) or "_(không có mô tả)_",
            "",
            "| Tham số | Kiểu | Bắt buộc | Mô tả |",
            "| --- | --- | --- | --- |",
        ]
        for param, spec in properties.items():
            lines.append(
                f"| `{param}` | {_param_type(spec)} | {'có' if param in required else 'không'} | "
                f"{_cell(str(spec.get('description', '')))} |"
            )
    return "\n".join(lines) + "\n"
