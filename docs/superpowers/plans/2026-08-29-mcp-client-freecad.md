# HCS MCP Client and FreeCAD Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add production MCP client support to HCS so Alexandria can connect to local stdio MCP servers, discover and invoke tools through HCS governance, and validate the integration against FreeCAD.

**Architecture:** Add a focused `hcs_ai/mcp_client.py` runtime manager backed by the official MCP Python SDK v2.1.1. The existing SQLite registry remains authoritative for configuration; runtime state stays in memory. The native tool layer becomes the single router for both built-in tools and namespaced MCP tools, while FastAPI and the Tkinter MCP tab expose lifecycle controls and diagnostics.

**Tech Stack:** Python 3.10+, Python 3.11 CI, FastAPI, Tkinter, SQLite, pytest/unittest, official `mcp` Python SDK v2.1.1, stdio transport.

**Spec:** `docs/superpowers/specs/2026-08-29-mcp-client-freecad-design.md`

## Global Constraints

- Phase 1 makes HCS an MCP client only; HCS-as-MCP-server is out of scope.
- Implement stdio transport first; do not add SSE or Streamable HTTP in this phase.
- Keep FreeCAD outside the MCP client core; it is a normal registered server and validation target.
- Preserve the existing `mcp_servers` SQLite table and `/mcp/servers` registry behavior.
- Runtime connection state is in memory, not persisted to SQLite.
- Every external tool is namespaced as `<server-name>.<tool-name>`.
- All MCP tool execution goes through an HCS policy hook and audit logging.
- Do not log credentials, environment values, or other secrets.
- A failed/crashed/malformed MCP server must never crash HCS.
- Use the official MCP SDK rather than implementing the wire protocol manually. Pin the initial runtime dependency to `mcp>=2.1.1,<3`.
- Existing Windows support and Python 3.10+ support must remain intact.

---

### Task 1: MCP SDK dependency and deterministic fake server

**Files:**
- Modify: `requirements.txt`
- Create: `tests/fixtures/fake_mcp_server.py`
- Create: `tests/test_mcp_client.py`

**Interfaces:**
- Consumes: Python subprocess execution and the MCP SDK.
- Produces: a deterministic stdio MCP server executable with modes `normal`, `slow`, and `crash`, plus initial red tests for `MCPClientManager`.

- [ ] **Step 1: Add the MCP SDK dependency**

Append this exact line to `requirements.txt`:

```text
mcp>=2.1.1,<3
```

- [ ] **Step 2: Create the fake stdio MCP server**

Create `tests/fixtures/fake_mcp_server.py` using the SDK's server API. It must expose these tools in normal mode:

```python
@server.tool()
def echo(text: str) -> str:
    return text

@server.tool()
def add(a: int, b: int) -> int:
    return a + b
```

Read `FAKE_MCP_MODE` from the child environment. `slow` must sleep long enough to exceed a 0.25-second client timeout when `echo` is called. `crash` must terminate the process during startup with exit code 17. Start the server on stdio when executed as `__main__`.

- [ ] **Step 3: Write the first failing client tests**

Create `tests/test_mcp_client.py` with `unittest.TestCase`. Define a registration helper returning:

```python
{
    "id": 1,
    "name": "fake",
    "transport": "stdio",
    "command": sys.executable,
    "args": [str(FAKE_SERVER)],
    "url": None,
    "enabled": True,
}
```

Add tests asserting that the not-yet-implemented manager will provide:

```python
manager.connect(registration)
status = manager.status("fake")
self.assertEqual(status["state"], "connected")
self.assertGreaterEqual(status["tool_count"], 2)
self.assertIn("fake.echo", manager.tool_specs())
self.assertEqual(manager.call_tool("fake.echo", {"text": "hello"})["text"], "hello")
manager.disconnect("fake")
self.assertEqual(manager.status("fake")["state"], "disconnected")
```

- [ ] **Step 4: Run the tests to prove the feature is absent**

Run:

```bash
python -m pytest tests/test_mcp_client.py -q
```

Expected: collection/import failure because `hcs_ai.mcp_client` does not exist.

- [ ] **Step 5: Commit the test fixture and dependency**

```bash
git add requirements.txt tests/fixtures/fake_mcp_server.py tests/test_mcp_client.py
git commit -m "test: define MCP client integration contract"
```

---

### Task 2: Implement stdio MCP runtime manager

**Files:**
- Create: `hcs_ai/mcp_client.py`
- Modify: `tests/test_mcp_client.py`

**Interfaces:**
- Consumes: registration dictionaries shaped like the existing `/mcp/servers` response.
- Produces:
  - `MCPClientManager(connect_timeout=5.0, tool_timeout=30.0)`
  - `connect(registration: dict) -> dict`
  - `disconnect(server_name: str) -> dict`
  - `shutdown() -> None`
  - `status(server_name: str | None = None) -> dict | list[dict]`
  - `refresh_tools(server_name: str) -> list[dict]`
  - `tool_specs() -> dict[str, dict]`
  - `call_tool(namespaced_name: str, args: dict) -> dict`
  - module singleton `manager`

- [ ] **Step 1: Add lifecycle tests**

Extend `tests/test_mcp_client.py` to cover reconnect and shutdown cleanup:

```python
manager.connect(registration)
manager.disconnect("fake")
manager.connect(registration)
self.assertEqual(manager.status("fake")["state"], "connected")
manager.shutdown()
self.assertEqual(manager.status("fake")["state"], "disconnected")
```

Also assert duplicate `connect()` is idempotent and returns the existing connected status.

- [ ] **Step 2: Implement a background asyncio runtime**

In `hcs_ai/mcp_client.py`, create a dedicated daemon thread that owns one asyncio event loop. Public manager methods remain synchronous and submit coroutines with `asyncio.run_coroutine_threadsafe()` so existing FastAPI sync endpoints and Tkinter code do not need to become async.

Define an internal runtime record with:

```python
@dataclass
class _ServerRuntime:
    registration: dict
    state: str = "disconnected"
    client: object | None = None
    context: object | None = None
    tools: dict[str, dict] = field(default_factory=dict)
    resources: list[dict] = field(default_factory=list)
    last_error: str = ""
    connected_at: str | None = None
```

- [ ] **Step 3: Implement stdio connection through the official SDK**

For `transport == "stdio"`, build:

```python
params = StdioServerParameters(
    command=registration["command"],
    args=list(registration.get("args") or []),
)
client = Client(params)
```

Enter the client's async context, perform normal initialization through the SDK lifecycle, then call the SDK's list-tools operation. Convert discovered tool metadata into plain dictionaries containing at least `name`, `description`, and JSON input schema.

Reject other transports with:

```python
raise ValueError("Phase 1 supports MCP stdio transport only")
```

- [ ] **Step 4: Implement namespacing and invocation**

Normalize the server namespace using lowercase alphanumeric characters, `_`, and `-`; replace spaces with `_`. Expose each tool as `f"{namespace}.{tool_name}"`.

`call_tool()` must split once on `.`, resolve the active runtime, enforce `tool_timeout`, call the SDK, and return a plain JSON-serializable dictionary with:

```python
{
    "server": server_name,
    "tool": tool_name,
    "is_error": bool(result.is_error),
    "content": [...],
    "structured_content": result.structured_content,
    "text": combined_text_content,
}
```

- [ ] **Step 5: Capture failures without killing HCS**

On connect/invoke exceptions, update `state` to `error` when the connection is unusable, store a concise `last_error`, close the SDK context when possible, and raise a bounded `RuntimeError` to the caller. `disconnect()` and `shutdown()` must be safe to call repeatedly.

- [ ] **Step 6: Run focused tests**

```bash
python -m pytest tests/test_mcp_client.py -q
```

Expected: normal lifecycle, discovery, invocation, reconnect, and shutdown tests pass.

- [ ] **Step 7: Commit**

```bash
git add hcs_ai/mcp_client.py tests/test_mcp_client.py
git commit -m "feat: add MCP stdio client manager"
```

---

### Task 3: Failure isolation, timeouts, resources, and policy hook

**Files:**
- Modify: `hcs_ai/mcp_client.py`
- Modify: `tests/fixtures/fake_mcp_server.py`
- Modify: `tests/test_mcp_client.py`
- Modify: `config.default.json`

**Interfaces:**
- Consumes: `MCPClientManager` from Task 2.
- Produces: `set_policy_hook(callable)`, resource discovery, conservative default policy, bounded timeout/crash behavior.

- [ ] **Step 1: Add explicit failure-mode tests**

Add tests for:

```python
with self.assertRaises(RuntimeError):
    manager.connect(crash_registration)
self.assertIn(manager.status("fake")["state"], {"error", "disconnected"})
```

and for timeout:

```python
manager = MCPClientManager(tool_timeout=0.25)
manager.connect(slow_registration)
with self.assertRaises(TimeoutError):
    manager.call_tool("fake.echo", {"text": "x"})
```

Add a policy test:

```python
manager.set_policy_hook(lambda server, tool, args: False)
with self.assertRaises(PermissionError):
    manager.call_tool("fake.echo", {"text": "blocked"})
```

- [ ] **Step 2: Add MCP defaults to `config.default.json`**

Add:

```json
"mcp": {
  "auto_connect_enabled": true,
  "connect_timeout_seconds": 5,
  "tool_timeout_seconds": 30,
  "default_tool_policy": "allow"
}
```

Keep this policy isolated behind the hook so the future HCS rules engine can replace it without touching transport code.

- [ ] **Step 3: Implement the policy hook**

Add:

```python
def set_policy_hook(self, hook):
    self._policy_hook = hook
```

Before every invocation, evaluate `hook(server_name, tool_name, args)`. A false result raises `PermissionError` before sending anything to the MCP server.

- [ ] **Step 4: Add resource discovery**

During connect/refresh, attempt the SDK resource-list operation. Servers that do not advertise resources should yield `[]`, not fail connection. Include resource count and resource metadata in runtime status/details.

- [ ] **Step 5: Make timeout and server-exit behavior bounded**

Wrap async operations with `asyncio.wait_for`. When the subprocess exits or protocol calls fail, update runtime state and preserve a concise last error. A timeout must fail only that operation; HCS itself must remain responsive.

- [ ] **Step 6: Run the failure-focused tests**

```bash
python -m pytest tests/test_mcp_client.py -q
```

Expected: crash, timeout, policy rejection, reconnect, and shutdown tests all pass.

- [ ] **Step 7: Commit**

```bash
git add hcs_ai/mcp_client.py config.default.json tests/fixtures/fake_mcp_server.py tests/test_mcp_client.py
git commit -m "feat: harden MCP lifecycle and policy gate"
```

---

### Task 4: Route MCP tools through the native HCS tool layer

**Files:**
- Modify: `hcs_ai/tools.py`
- Modify: `hcs_ai/llm.py`
- Create: `tests/test_mcp_tool_router.py`

**Interfaces:**
- Consumes: singleton `hcs_ai.mcp_client.manager`.
- Produces: MCP-aware `public_tool_specs()` and `call_tool()` while preserving existing native tool behavior.

- [ ] **Step 1: Write router tests**

Create `tests/test_mcp_tool_router.py`. Patch the manager so:

```python
manager.tool_specs.return_value = {
    "freecad.create_box": {
        "name": "freecad.create_box",
        "description": "Create a box",
        "schema": {
            "type": "object",
            "properties": {"length": {"type": "number"}},
            "required": ["length"],
        },
    }
}
```

Assert `public_tool_specs()` includes both native `system_info` and `freecad.create_box`, and assert `call_tool("freecad.create_box", {"length": 10})` delegates to `manager.call_tool()`.

- [ ] **Step 2: Extend the public tool catalog**

Update `public_tool_specs()` to append connected MCP tool specs. Preserve native schemas as-is. For MCP tools, retain their real JSON Schema instead of flattening it to HCS's current string-description format.

- [ ] **Step 3: Extend `call_tool()`**

Keep native tools first:

```python
if name in TOOLS:
    ...existing native behavior...
```

Otherwise, if the name exactly matches a connected MCP namespaced tool, audit and call `mcp_client.manager.call_tool(name, args)`. Unknown names still raise `KeyError`.

- [ ] **Step 4: Teach LLM tool-definition conversion to accept real JSON Schema**

In `hcs_ai/llm.py`, modify `_tool_definitions()` so if `t["schema"]` already contains `type == "object"` and `properties`, it passes that schema through unchanged. Otherwise use the existing conversion for native HCS string schemas.

- [ ] **Step 5: Make explicit tool access include MCP tools**

The existing phrases `all tools`, `full tool access`, `use any tool`, and `available tools` must return connected MCP tools as well as native tools. Do not add broad semantic auto-selection for arbitrary MCP tools yet; explicit access is sufficient for Phase 1 and avoids bloating every local-model prompt.

- [ ] **Step 6: Run router and existing LLM tests**

```bash
python -m pytest tests/test_mcp_tool_router.py tests/test_llm_telemetry.py tests/test_recent_features.py -q
```

Expected: all pass with existing native behavior preserved.

- [ ] **Step 7: Commit**

```bash
git add hcs_ai/tools.py hcs_ai/llm.py tests/test_mcp_tool_router.py
git commit -m "feat: route namespaced MCP tools through HCS"
```

---

### Task 5: Add MCP runtime API and audit lifecycle

**Files:**
- Modify: `hcs_ai/server.py`
- Create: `tests/test_mcp_api.py`

**Interfaces:**
- Consumes: existing MCP SQLite registry and singleton manager.
- Produces:
  - `GET /mcp/status`
  - `POST /mcp/servers/{name}/connect`
  - `POST /mcp/servers/{name}/disconnect`
  - `POST /mcp/servers/{name}/refresh`
  - `DELETE /mcp/servers/{name}`
  - `POST /mcp/servers/{name}/enabled`

- [ ] **Step 1: Write API behavior tests**

Use FastAPI `TestClient` with the manager patched. Assert that `/mcp/status` returns registry configuration merged with runtime fields `state`, `tool_count`, `resource_count`, and `last_error`.

Assert connect looks up the named registry row before calling the manager, rather than accepting an arbitrary executable in the request body.

- [ ] **Step 2: Add registry lookup helper**

In `server.py`, add a private helper returning one normalized server row by name. Missing names raise HTTP 404.

- [ ] **Step 3: Add lifecycle endpoints**

Connect uses the persisted registration. Disconnect and refresh address a registered server by name. Enable/disable updates only the durable `enabled` flag. Remove first disconnects, then deletes the registry row.

- [ ] **Step 4: Auto-connect enabled servers on startup**

After `init_db()` and without delaying HCS startup indefinitely, iterate enabled stdio registrations and request manager connections. Individual failures must be audited and ignored so the main HCS server still starts.

- [ ] **Step 5: Shut down MCP before process exit**

In the FastAPI shutdown handler call:

```python
mcp_client.manager.shutdown()
engine.stop()
```

A manager shutdown exception must be audited but must not prevent engine cleanup.

- [ ] **Step 6: Audit lifecycle and tool outcomes**

Use existing `audit()` calls for connect attempt/success/failure, disconnect, refresh, removal, policy rejection, and tool invocation result. Log server/tool names, durations, and error summaries; do not serialize environment variables or credential-bearing configuration.

- [ ] **Step 7: Run API tests**

```bash
python -m pytest tests/test_mcp_api.py tests/test_mcp_client.py tests/test_mcp_tool_router.py -q
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add hcs_ai/server.py tests/test_mcp_api.py
git commit -m "feat: expose MCP runtime controls through HCS API"
```

---

### Task 6: Upgrade the MCP Tkinter tab into a control center

**Files:**
- Modify: `hcs_ai/gui.py`
- Create: `tests/test_gui_mcp.py`

**Interfaces:**
- Consumes: MCP registry/runtime API from Task 5.
- Produces: operational MCP tab with server list, status, controls, and discovery details.

- [ ] **Step 1: Add source-level GUI contract tests**

Following the repository's existing GUI test style, create `tests/test_gui_mcp.py` using `inspect.getsource(App.build_mcp)` and assert the implementation contains controls labelled:

```text
Add/Edit
Connect
Disconnect
Enable/Disable
Refresh Tools
Remove
```

Also assert it creates a `ttk.Treeview` and a detail `tk.Text` area.

- [ ] **Step 2: Replace the raw JSON MCP display**

Build a horizontal paned layout. Left side: Treeview columns `Name`, `Transport`, `Enabled`, `State`, `Tools`, `Status`. Right side: read-only text showing discovered tools/resources and their schemas for the selected server.

- [ ] **Step 3: Implement server selection and refresh**

`refresh_mcp()` calls `GET /mcp/status`, repopulates the tree, preserves selection where possible, and updates detail text. Do not display raw credential/environment values.

- [ ] **Step 4: Implement controls against the API**

Wire Connect/Disconnect/Refresh/Remove/Enable to Task 5 endpoints. Reuse the existing Add registration flow, renaming the button to `Add/Edit` and preserving stdio command/args entry.

- [ ] **Step 5: Add concise error presentation**

API errors use `messagebox.showerror("MCP", ...)`; runtime errors also remain visible in the row's status/last-error column after refresh.

- [ ] **Step 6: Run GUI tests and compile check**

```bash
python -m pytest tests/test_gui_mcp.py tests/test_gui_recent.py -q
python -m compileall -q hcs_ai
```

Expected: tests pass and compile produces no errors.

- [ ] **Step 7: Commit**

```bash
git add hcs_ai/gui.py tests/test_gui_mcp.py
git commit -m "feat: add MCP server control center"
```

---

### Task 7: Full regression verification and FreeCAD validation guide

**Files:**
- Modify: `README.md`
- Create: `docs/MCP_FREECAD_SETUP.md`

**Interfaces:**
- Consumes: completed generic MCP client.
- Produces: reproducible real-FreeCAD validation procedure without hard-coding a particular FreeCAD MCP tool schema into HCS.

- [ ] **Step 1: Run the full automated suite before touching FreeCAD**

```bash
python -m compileall -q hcs_ai
python -m pytest -q
```

Expected: entire repository test suite passes.

- [ ] **Step 2: Document FreeCAD registration procedure**

Create `docs/MCP_FREECAD_SETUP.md` with these required steps:

1. Install FreeCAD separately.
2. Select a compatible FreeCAD MCP server implementation.
3. Determine its stdio launch command and arguments from that server's documentation.
4. In HCS MCP tab choose `Add/Edit`, set name `freecad`, transport `stdio`, command, and args.
5. Enable and Connect.
6. Confirm State = Connected and Tools > 0.
7. Inspect discovered tool schemas in the detail panel rather than assuming tool names.
8. Invoke one read-only/inspection operation exposed by that server.
9. Invoke one model-changing operation exposed by that server.
10. Visually or machine-verifiably confirm the result in FreeCAD.
11. Disconnect and confirm HCS remains running.

State explicitly that HCS does not install or trust arbitrary MCP server code automatically.

- [ ] **Step 3: Update README feature list**

Replace the old implication that MCP is only a registry with a concise description that HCS can connect to local stdio MCP servers, dynamically discover tools/resources, namespace tools, and route calls through HCS audit/policy handling.

- [ ] **Step 4: Run final regression suite again**

```bash
python -m compileall -q hcs_ai
python -m pytest -q
```

Expected: all tests pass after documentation-only changes.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/MCP_FREECAD_SETUP.md
git commit -m "docs: add FreeCAD MCP validation guide"
```

---

## Final Acceptance Checklist

- [ ] `python -m compileall -q hcs_ai` succeeds.
- [ ] `python -m pytest -q` succeeds.
- [ ] Enabled stdio servers can connect without blocking HCS startup indefinitely.
- [ ] Discovery returns real MCP tools and optional resources.
- [ ] Tool names are namespaced and appear in HCS's public tool catalog.
- [ ] Tool invocation passes through the policy hook and audit path.
- [ ] Timeouts, process crashes, and protocol failures are isolated to the affected server.
- [ ] Reconnect and repeated disconnect are safe.
- [ ] HCS shutdown closes MCP clients before stopping the local inference engine.
- [ ] MCP GUI exposes Add/Edit, Connect, Disconnect, Enable/Disable, Refresh Tools, and Remove.
- [ ] FreeCAD uses the same generic registry/discovery path as every other MCP server.
