"""MCP configuration tests without requiring a network server."""
from pathlib import Path

import pytest

from minion.runtime.mcp_tools import MCPManager


def test_mcp_config_is_operator_owned_and_validated(
    tmp_path: Path,
):
    config = tmp_path / "mcp.yaml"
    config.write_text(
        "servers:\n"
        "  local-tools:\n"
        "    url: http://localhost:9000/mcp\n"
        "    description: Local integration test server.\n"
    )

    manager = MCPManager(config)
    assert manager.catalog() == [
        {
            "name": "local_tools",
            "description": "Local integration test server.",
        }
    ]


def test_remote_plain_http_mcp_is_rejected(
    tmp_path: Path,
):
    config = tmp_path / "mcp.yaml"
    config.write_text(
        "servers:\n"
        "  unsafe:\n"
        "    url: http://example.com/mcp\n"
    )

    with pytest.raises(ValueError, match="must use HTTPS"):
        MCPManager(config).servers()
