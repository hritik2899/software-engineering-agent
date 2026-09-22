"""Operator-configured Model Context Protocol integration.

MCP extends the agent with tools from explicitly trusted servers without baking every
external integration into this repository. Configuration is control-plane owned:
repository files and Agent Skills cannot add or replace MCP endpoints.

All discovered MCP tools are namespaced as mcp__<server>__<tool>. Because a remote
tool may have side effects that its JSON schema cannot fully describe, MCP calls are
conservatively treated as sequential/mutating by the CodingAgent.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from mcp import Client


@dataclass(slots=True, frozen=True)
class MCPServerConfig:
    """One explicitly approved Streamable HTTP MCP endpoint."""

    name: str
    url: str
    description: str = ""


class MCPManager:
    """Discover schemas and execute calls against configured MCP servers."""

    def __init__(self, config_path: Path | None):
        self.config_path = config_path
        self._servers: dict[str, MCPServerConfig] | None = None
        self._schema_cache: list[dict[str, Any]] | None = None
        self._tool_routes: dict[str, tuple[str, str]] = {}

    @staticmethod
    def _safe_name(value: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")
        if not safe:
            raise ValueError("MCP server/tool name cannot be empty")
        return safe

    def servers(self) -> dict[str, MCPServerConfig]:
        if self._servers is not None:
            return self._servers
        if self.config_path is None:
            self._servers = {}
            return self._servers

        path = self.config_path.expanduser()
        if not path.exists():
            self._servers = {}
            return self._servers

        raw = yaml.safe_load(path.read_text()) or {}
        items = raw.get("servers", {})
        if not isinstance(items, dict):
            raise ValueError("MCP config 'servers' must be a mapping")

        servers: dict[str, MCPServerConfig] = {}
        for raw_name, value in items.items():
            if not isinstance(value, dict):
                raise ValueError(
                    f"MCP server {raw_name!r} must be a mapping"
                )
            name = self._safe_name(str(raw_name))
            url = str(value.get("url", "")).strip()
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(
                    f"MCP server {raw_name!r} needs an http(s) URL"
                )
            if (
                parsed.scheme == "http"
                and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            ):
                raise ValueError(
                    f"MCP server {raw_name!r} must use HTTPS outside localhost"
                )
            servers[name] = MCPServerConfig(
                name=name,
                url=url,
                description=str(value.get("description", "")).strip(),
            )

        self._servers = servers
        return servers

    def catalog(self) -> list[dict[str, str]]:
        """Return public endpoint metadata without exposing URL secrets."""
        return [
            {
                "name": server.name,
                "description": server.description,
            }
            for server in self.servers().values()
        ]

    async def schemas(self) -> list[dict[str, Any]]:
        """Discover remote MCP tools once per runtime and namespace them."""
        if self._schema_cache is not None:
            return self._schema_cache

        schemas: list[dict[str, Any]] = []
        routes: dict[str, tuple[str, str]] = {}

        for server in self.servers().values():
            async with Client(server.url) as client:
                result = await client.list_tools()
                for tool in result.tools:
                    exposed_name = (
                        f"mcp__{server.name}__"
                        f"{self._safe_name(tool.name)}"
                    )
                    routes[exposed_name] = (
                        server.name,
                        tool.name,
                    )
                    schemas.append(
                        {
                            "type": "function",
                            "function": {
                                "name": exposed_name,
                                "description": (
                                    f"[MCP:{server.name}] "
                                    f"{tool.description or tool.name}"
                                ),
                                "parameters": (
                                    tool.input_schema
                                    if isinstance(tool.input_schema, dict)
                                    else {
                                        "type": "object",
                                        "properties": {},
                                    }
                                ),
                            },
                        }
                    )

        self._tool_routes = routes
        self._schema_cache = schemas
        return schemas

    async def call(
        self,
        exposed_name: str,
        arguments: dict[str, Any],
    ) -> tuple[bool, str]:
        """Execute a previously discovered namespaced MCP tool."""
        if self._schema_cache is None:
            await self.schemas()
        route = self._tool_routes.get(exposed_name)
        if route is None:
            return False, f"unknown MCP tool: {exposed_name}"

        server_name, remote_tool_name = route
        server = self.servers()[server_name]

        async with Client(server.url) as client:
            result = await client.call_tool(
                remote_tool_name,
                arguments,
            )

        payload: dict[str, Any] = {
            "is_error": bool(getattr(result, "is_error", False)),
            "structured_content": getattr(
                result, "structured_content", None
            ),
            "content": [
                (
                    item.model_dump(mode="json")
                    if hasattr(item, "model_dump")
                    else str(item)
                )
                for item in getattr(result, "content", [])
            ],
        }
        return (
            not payload["is_error"],
            json.dumps(payload, ensure_ascii=False)[:30_000],
        )
