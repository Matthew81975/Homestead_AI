import sys
import unittest
from pathlib import Path

from hcs_ai.mcp_client import MCPClientManager


FAKE_SERVER = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


def registration(mode="normal"):
    return {
        "id": 1,
        "name": "fake",
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(FAKE_SERVER)],
        "url": None,
        "enabled": True,
        "env": {"FAKE_MCP_MODE": mode},
    }


class MCPClientManagerTests(unittest.TestCase):
    def tearDown(self):
        manager = getattr(self, "manager", None)
        if manager is not None:
            manager.shutdown()

    def test_connect_discovers_tools_and_invokes_namespaced_tool(self):
        self.manager = MCPClientManager()
        self.manager.connect(registration())

        status = self.manager.status("fake")
        self.assertEqual(status["state"], "connected")
        self.assertGreaterEqual(status["tool_count"], 2)
        self.assertIn("fake.echo", self.manager.tool_specs())

        result = self.manager.call_tool("fake.echo", {"text": "hello"})
        self.assertEqual(result["text"], "hello")

    def test_disconnect_and_reconnect_are_safe(self):
        self.manager = MCPClientManager()
        self.manager.connect(registration())
        self.manager.disconnect("fake")
        self.assertEqual(self.manager.status("fake")["state"], "disconnected")

        self.manager.connect(registration())
        self.assertEqual(self.manager.status("fake")["state"], "connected")

    def test_shutdown_disconnects_managed_server(self):
        self.manager = MCPClientManager()
        self.manager.connect(registration())
        self.manager.shutdown()
        self.assertEqual(self.manager.status("fake")["state"], "disconnected")


if __name__ == "__main__":
    unittest.main()
