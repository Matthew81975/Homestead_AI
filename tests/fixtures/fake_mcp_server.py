import os
import sys
import time

from mcp.server import MCPServer

mcp = MCPServer("HCS Fake MCP")


@mcp.tool()
def echo(text: str) -> str:
    """Return text unchanged; optionally delay for timeout tests."""
    if os.environ.get("FAKE_MCP_MODE", "normal") == "slow":
        time.sleep(1.0)
    return text


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


if __name__ == "__main__":
    if os.environ.get("FAKE_MCP_MODE", "normal") == "crash":
        raise SystemExit(17)
    mcp.run()
