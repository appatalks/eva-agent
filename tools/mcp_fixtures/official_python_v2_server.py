"""Minimal official MCP Python SDK v2 stdio server for Eva interoperability tests."""

from mcp.server import MCPServer


server = MCPServer("eva-official-python-fixture", version="2.0.0")


@server.tool()
def official_echo(value: str) -> str:
    """Return deterministic text to prove Eva completed a modern MCP tool call."""
    return "official:" + value


if __name__ == "__main__":
    server.run(transport="stdio")