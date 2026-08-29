# HCS MCP Client and FreeCAD Integration Design

## Purpose

Add real Model Context Protocol (MCP) client capability to HCS so Alexandria can connect to external MCP servers through HCS, discover their capabilities dynamically, and invoke them under HCS rules, logging, and lifecycle control. FreeCAD is the first target integration, but the subsystem must remain generic so future MCP servers can be added without application-specific changes to the core client.

## Scope

Phase 1 covers HCS acting as an MCP client. HCS acting as an MCP server is explicitly out of scope for this phase.

The first transport is local stdio. Streamable HTTP is deferred to a later phase but the client interface must not prevent its addition.

The first real external integration target is FreeCAD through a compatible FreeCAD MCP server. FreeCAD must be represented as a normal MCP server registration rather than being hard-coded into the client core.

## Existing HCS Context

HCS already has:

- a persistent `mcp_servers` registry in SQLite,
- `/mcp/servers` GET and POST endpoints,
- an MCP tab in the Tkinter GUI,
- a native HCS tool catalog and tool-call endpoint,
- persistent audit logging,
- local AI runtime lifecycle management,
- rules/security concepts that remain authoritative over external integrations.

The current MCP GUI describes itself as a V1 registry and does not yet perform MCP transport, initialization, capability discovery, or tool execution.

## Architecture

Introduce a focused `MCPClientManager` subsystem behind the existing MCP registry and GUI.

Primary data flow:

`Alexandria -> HCS tool router -> HCS rules/permission gate -> MCPClientManager -> MCP server -> external application`

FreeCAD data flow:

`Alexandria -> HCS -> FreeCAD MCP server -> FreeCAD`

HCS remains the authoritative control layer. External MCP servers never bypass HCS policy, audit, or lifecycle handling.

The MCP client core must remain independent of FreeCAD. FreeCAD-specific setup may be represented by an optional registry preset, but discovered tools and resources are always learned dynamically from the server.

## Components

### MCPClientManager

Responsibilities:

- load enabled server registrations,
- start local stdio MCP servers as child processes,
- perform MCP initialization and capability negotiation,
- discover tools and resources exposed by each server,
- cache runtime connection state and discovered schemas,
- invoke MCP tools,
- expose connection state and errors to the HCS API and GUI,
- capture useful stderr from child processes,
- detect server exit/crash,
- cleanly disconnect and terminate child processes,
- shut down all managed servers when HCS exits.

The manager must isolate server failures from the HCS process. A crashed or malformed MCP server is marked failed/disconnected; it must not terminate HCS.

### Transport abstraction

Phase 1 implements stdio only. The higher-level manager interface must be transport-neutral so Streamable HTTP can be added later without changing tool routing or GUI semantics.

The stdio transport owns:

- child-process launch,
- stdin/stdout message exchange,
- stderr capture,
- request IDs and response correlation,
- timeout handling,
- child-process termination.

### MCP registry

Preserve the existing SQLite registration model as the source of configuration. A registration includes at least:

- server name,
- transport,
- command,
- argument list,
- URL where applicable for future transports,
- enabled state.

Runtime-only state such as connected/disconnected, process ID, discovered tool count, and last error should not be treated as durable configuration.

### HCS tool routing

Discovered MCP tools become available through the normal HCS tool layer under a collision-resistant namespace:

`<server-name>.<tool-name>`

Examples:

- `freecad.create_box`
- `freecad.create_sketch`
- `freecad.export_model`

The exact FreeCAD tool names are not assumed by HCS; they come from MCP discovery.

The router determines whether a requested tool is a native HCS tool or a namespaced MCP tool and dispatches accordingly.

## Permissions and policy

All MCP calls pass through HCS governance before execution.

The design must support policy classes such as:

1. read/inspect,
2. modify external data or models,
3. filesystem/process execution,
4. physical-machine/control operations.

Phase 1 only needs the enforcement hook and conservative defaults; it does not require a complete future rules-engine UI.

Per-server and per-tool policy overrides must be possible later without changing the MCP protocol layer.

Connecting to a server and discovering capabilities may be automatic for enabled servers. Consequential tool execution remains governed by HCS policy.

## Audit and diagnostics

Record at least:

- server connect attempts,
- successful initialization,
- disconnects,
- crashes/exits,
- discovery refreshes,
- tool invocation requests,
- tool results,
- tool failures,
- duration/timing,
- policy rejection where applicable.

Diagnostics should distinguish failures by layer where possible:

- HCS registry/configuration,
- transport/process,
- MCP protocol,
- MCP server,
- external application/tool.

Do not log secrets or raw credentials.

## GUI

Upgrade the existing MCP tab from a raw JSON registry display into a basic control center.

For each registered server show:

- name,
- transport,
- enabled state,
- runtime connection state,
- discovered tool count,
- last error or concise status.

Provide controls for:

- Add/Edit server,
- Connect,
- Disconnect,
- Enable/Disable,
- Refresh Tools,
- Remove server.

Selecting a server should show discovered MCP tools/resources and available schemas in a readable detail panel.

A FreeCAD setup preset may prefill registry configuration after a specific compatible FreeCAD MCP implementation is chosen, but FreeCAD must not receive a special execution path in the client core.

## FreeCAD integration boundary

FreeCAD is the first integration validation target after the generic MCP client passes isolated tests.

HCS will connect to a FreeCAD MCP server through the same stdio registration and discovery mechanism used for any other server.

No HCS code should assume specific FreeCAD tool names. HCS should display and route whatever capabilities the selected FreeCAD MCP server advertises.

The FreeCAD validation should demonstrate at minimum:

- server connection,
- capability discovery,
- at least one read/inspection call if exposed,
- at least one model-modification call if exposed,
- a visible or machine-verifiable result,
- clean disconnect/shutdown.

## Failure handling

A single MCP server failure must not crash HCS.

Required behaviors:

- connection timeout produces a bounded error,
- malformed protocol response produces a protocol error and isolates that server,
- server process exit updates status and captures useful stderr,
- tool timeout fails that invocation without hanging the HCS server,
- reconnect is possible after failure,
- HCS shutdown terminates child MCP processes cleanly,
- duplicate tool names across servers remain unambiguous because of namespacing.

## Testing strategy

Before using real FreeCAD, create a minimal fake stdio MCP server under tests.

The fake server must allow deterministic testing of:

- initialization handshake,
- tool discovery,
- tool invocation,
- namespacing,
- malformed response handling,
- request timeout,
- server crash/exit,
- reconnect,
- shutdown cleanup,
- policy rejection hook.

Only after these tests pass should real FreeCAD integration be attempted. This separates HCS MCP defects from FreeCAD installation/configuration defects.

## Implementation boundaries

Likely focused code units:

- new MCP client/transport module for protocol and process lifecycle,
- small tool-router integration in the existing native tool layer,
- API endpoints in `hcs_ai/server.py` for runtime status/connect/disconnect/discovery/invoke operations,
- MCP-tab upgrade in `hcs_ai/gui.py`,
- deterministic fake MCP server and unit/integration tests,
- dependency/configuration changes only where required by the chosen protocol implementation.

Avoid broad unrelated refactors. The existing HCS registry, audit system, tool catalog, GUI style, and server structure should be reused.

## Phase 1 completion criteria

Phase 1 is complete when:

- HCS can connect to an enabled local stdio MCP server,
- initialization succeeds and status is visible,
- tools are discovered dynamically,
- discovered tools are exposed to HCS using server namespaces,
- a discovered tool can be invoked through the HCS tool path,
- audit logging captures the operation,
- server crashes/timeouts do not crash HCS,
- reconnect and shutdown cleanup work,
- the upgraded MCP GUI provides operational controls and useful status,
- fake-server tests pass,
- a selected FreeCAD MCP server can be registered, connected, discovered, and used for at least one validated operation.

## Deferred work

The following are explicitly deferred:

- HCS acting as an MCP server,
- Streamable HTTP transport,
- remote credential/OAuth flows,
- full graphical policy editor,
- automatic installation of arbitrary MCP servers,
- FreeCAD-specific semantic planning beyond generic MCP tool use.
