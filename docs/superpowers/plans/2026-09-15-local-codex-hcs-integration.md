# Local Codex HCS Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import Local Codex 2.10.8 as a native HCS subsystem, supervise it through a resilient worker process, and add a full-control Tkinter tab with live structured logs.

**Architecture:** Local Codex code lives under `hcs_ai/local_codex/`, but task execution occurs in an HCS-owned child process using a versioned newline-delimited JSON protocol. `LocalCodexService` is the sole GUI-facing facade; it owns lifecycle, typed commands, ordered/redacted events, persistent logs, and recovery. A focused GUI mixin renders the control surface without further inflating `hcs_ai/gui.py`.

**Tech Stack:** Python 3.11+, Tkinter/ttk, stdlib `subprocess`, `threading`, `queue`, `json`, existing `psutil`, pytest.

**Spec:** `docs/superpowers/specs/2026-09-15-local-codex-hcs-integration-design.md`

## Global Constraints

- Target HCS release is `0.11.0`; imported Local Codex source is version `2.10.8`.
- Local Codex runs from `hcs_ai.local_codex` and HCS owns its update lifecycle; the imported self-updater must never replace HCS files.
- The GUI communicates only through `LocalCodexService`; it never calls `ActionExecutor` directly.
- The worker protocol is newline-delimited JSON with `protocol_version = 1` and monotonically increasing event sequence numbers.
- Only one Local Codex task may execute at a time; a second submission returns a typed `busy` result.
- Workspace, action, commit, push, deletion, credential, and other safety boundaries remain authoritative.
- Mutable state lives below `ROOT/data/local_codex/`; local configuration remains ignored and secrets never enter tracked files, receipts, events, or ordinary logs.
- Hiding HCS to the tray leaves Local Codex running; full Exit or Restart stops the worker and descendants.
- The standalone Local Codex installation is read-only during one-time migration and remains the rollback copy.
- Existing HCS 0.10.0 configuration files and existing Local Codex state remain loadable.

---

## File Structure

- Create `hcs_ai/local_codex/` — imported Local Codex runtime with package-relative imports.
- Create `hcs_ai/local_codex/runtime.py` — noninteractive task construction and execution API shared by worker and compatibility CLI.
- Create `hcs_ai/local_codex/protocol.py` — typed command/event validation and redaction primitives.
- Create `hcs_ai/local_codex/worker.py` — NDJSON worker loop and task thread.
- Create `hcs_ai/local_codex/service.py` — HCS-side child-process supervision and event queue.
- Create `hcs_ai/local_codex/migration.py` — idempotent standalone-install discovery and migration.
- Create `hcs_ai/gui_local_codex.py` — Local Codex tab mixin and pure formatting helpers.
- Modify `hcs_ai/gui.py`, `hcs_ai/gui_tree.py`, and `hcs_ai/desktop_host.py` — tab construction and host lifecycle integration.
- Modify configuration, installer/update manifest, version, README, and release notes for HCS 0.11.0.
- Port Local Codex tests under `tests/local_codex/`; add HCS integration tests under `tests/`.

---

### Task 1: Import Local Codex Runtime into the HCS Namespace

**Files:**
- Create: `hcs_ai/local_codex/__init__.py`
- Create: `hcs_ai/local_codex/actions.py`
- Create: `hcs_ai/local_codex/controller.py`
- Create: `hcs_ai/local_codex/credentials.py`
- Create: `hcs_ai/local_codex/email_worker.py`
- Create: `hcs_ai/local_codex/git_workflow.py`
- Create: `hcs_ai/local_codex/github_handoff.py`
- Create: `hcs_ai/local_codex/lm_client.py`
- Create: `hcs_ai/local_codex/mail_gateway.py`
- Create: `hcs_ai/local_codex/models.py`
- Create: `hcs_ai/local_codex/project_control.py`
- Create: `hcs_ai/local_codex/prompt_optimizer.py`
- Create: `hcs_ai/local_codex/state.py`
- Create: `hcs_ai/local_codex/task_queue.py`
- Create: `hcs_ai/local_codex/tornado.py`
- Create: `hcs_ai/local_codex/worker_instance.py`
- Create: `hcs_ai/local_codex/workspace.py`
- Create: `hcs_ai/local_codex/workspace_registry.py`
- Create: `hcs_ai/local_codex/runtime.py`
- Create: `tests/local_codex/` ported tests
- Test: `tests/test_local_codex_import.py`

**Interfaces:**
- Consumes: Local Codex 2.10.8 source snapshot.
- Produces: `hcs_ai.local_codex.VERSION == "2.10.8"` and importable runtime modules.
- Produces: `run_task(config, journal, *, dry_run=False, workspaces_config_path, status_callback, approval_callback=None, cancel_event=None) -> AgentStatus` in `runtime.py`.

- [ ] **Step 1: Add a failing namespace/runtime contract test**

```python
from pathlib import Path

from hcs_ai.local_codex import VERSION
from hcs_ai.local_codex.runtime import run_task


def test_local_codex_runtime_is_native_hcs_package():
    assert VERSION == "2.10.8"
    assert run_task.__module__ == "hcs_ai.local_codex.runtime"
    assert Path(__import__("hcs_ai.local_codex.tornado", fromlist=["x"]).__file__).parts[-3:-1] == (
        "hcs_ai", "local_codex"
    )
```

- [ ] **Step 2: Run the contract test and verify RED**

Run: `python -m pytest tests/test_local_codex_import.py -q`

Expected: collection fails with `ModuleNotFoundError: No module named 'hcs_ai.local_codex'`.

- [ ] **Step 3: Copy and namespace the 2.10.8 runtime**

Copy the runtime modules from the supplied 2.10.8 snapshot into `hcs_ai/local_codex/`. Replace imports of `agent.<module>` with relative imports such as:

```python
from .actions import ActionExecutor, CommandPolicy
from .controller import AgentController
from .state import AgentStatus, TaskJournal
```

Set `hcs_ai/local_codex/__init__.py` to:

```python
VERSION = "2.10.8"
```

Do not import the standalone `main.py`, launcher scripts, desktop shortcuts, or standalone self-update execution path. Port `self_update.py` only if another imported module requires its compatibility types; any callable update entry point must raise `RuntimeError("Local Codex updates are managed by HCS")`.

- [ ] **Step 4: Extract the noninteractive runtime API**

Move the construction logic from standalone `run_task` into `runtime.py`. Route status through the supplied callback, defaulting to a no-op rather than `print`. Check `cancel_event` between controller actions; do not weaken `ActionExecutor` workspace checks.

```python
def run_task(config, journal, *, dry_run=False, workspaces_config_path, status_callback=None,
             approval_callback=None, cancel_event=None):
    status = status_callback or (lambda _message: None)
    controller = build_controller(
        config=config,
        journal=journal,
        dry_run=dry_run,
        workspaces_config_path=workspaces_config_path,
        status_callback=status,
        approval_callback=approval_callback,
        cancel_event=cancel_event,
    )
    return controller.run().status
```

`build_controller(...)` performs the former standalone construction of `Workspace`, model client, optional `ProjectControlClient`, `ActionExecutor`, and `AgentController`. Extend `AgentController` with optional `cancel_event`; at the top of each action-loop iteration, a set event returns a stopped result after saving the current journal.

- [ ] **Step 5: Port the Local Codex tests**

Copy behavioral tests into `tests/local_codex/`, rewrite imports from `agent` to `hcs_ai.local_codex`, remove tests that assert standalone shortcut/updater packaging, and keep all controller, action, Tornado, workspace, queue, mail, Git, journal, and prompt-optimizer tests.

- [ ] **Step 6: Verify the imported runtime**

Run: `python -m pytest tests/local_codex tests/test_local_codex_import.py -q`

Expected: all retained Local Codex tests and the namespace contract pass.

- [ ] **Step 7: Commit**

```bash
git add hcs_ai/local_codex tests/local_codex tests/test_local_codex_import.py
git commit -m "feat: import Local Codex runtime into HCS"
```

---

### Task 2: Add Versioned Protocol, Redaction, and Persistent Event Logging

**Files:**
- Create: `hcs_ai/local_codex/protocol.py`
- Test: `tests/test_local_codex_protocol.py`

**Interfaces:**
- Consumes: no Task 1 internals.
- Produces: `PROTOCOL_VERSION = 1`.
- Produces: `Command.from_json(line)`, `Event.to_json()`, `EventFactory.emit(type, level="info", payload=None)`.
- Produces: `redact(value, *, secret_names=(), secret_values=())`.

- [ ] **Step 1: Write failing protocol tests**

```python
def test_event_factory_emits_monotonic_sequence_and_required_envelope():
    factory = EventFactory(clock=lambda: 1000.0)
    first = factory.emit("worker_started")
    second = factory.emit("log", payload={"message": "ready"})
    assert first.sequence == 1
    assert second.sequence == 2
    assert json.loads(second.to_json()) == {
        "protocol_version": 1, "sequence": 2, "timestamp": 1000.0,
        "type": "log", "level": "info", "payload": {"message": "ready"},
    }


def test_command_rejects_unknown_version_and_type():
    with pytest.raises(ProtocolError, match="protocol_version"):
        Command.from_json('{"protocol_version":2,"command_id":"c1","type":"status","payload":{}}')
    with pytest.raises(ProtocolError, match="unknown command"):
        Command.from_json('{"protocol_version":1,"command_id":"c1","type":"explode","payload":{}}')


def test_redaction_removes_nested_secret_names_values_and_bearer_tokens():
    value = {"password": "hunter2", "nested": ["Bearer abc123", "safe abc123 text"]}
    assert redact(value, secret_values=("abc123",)) == {
        "password": "[REDACTED]",
        "nested": ["Bearer [REDACTED]", "safe [REDACTED] text"],
    }
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_local_codex_protocol.py -q`

Expected: import failure because `protocol.py` does not exist.

- [ ] **Step 3: Implement strict dataclasses and validators**

Accept only command types `submit_task`, `resume_task`, `stop_task`, `approve`, `status`, and `shutdown`. Require a nonempty `command_id`, object payload, exact integer protocol version, and type-specific fields. Events may only contain JSON-safe redacted data.

- [ ] **Step 4: Implement log rotation**

Add `EventLog(path, *, max_bytes=2_000_000, backups=5)` to append one redacted JSON event per line. Rotate `local-codex.log` through `.1` to `.5` before an append would exceed the limit. A logging failure returns a warning event rather than raising into the GUI.

- [ ] **Step 5: Run focused tests and commit**

Run: `python -m pytest tests/test_local_codex_protocol.py -q`

```bash
git add hcs_ai/local_codex/protocol.py tests/test_local_codex_protocol.py
git commit -m "feat: add Local Codex worker protocol and safe logs"
```

---

### Task 3: Implement Idempotent Standalone Migration

**Files:**
- Create: `hcs_ai/local_codex/migration.py`
- Modify: `.gitignore`
- Modify: `config.default.json`
- Test: `tests/test_local_codex_migration.py`

**Interfaces:**
- Produces: `MigrationResult(source_path, source_version, imported_categories, warnings, skipped)`.
- Produces: `discover_legacy_install(candidates) -> Path | None`.
- Produces: `migrate_legacy_install(*, candidates, data_root, local_config_path, clock=time.time) -> MigrationResult`.
- Stores receipt at `ROOT/data/local_codex/migration/receipt.json`.

- [ ] **Step 1: Write failing discovery and migration tests**

```python
def test_discovery_selects_highest_valid_semantic_version(tmp_path):
    make_legacy(tmp_path / "Local_Codex_Agent_v2.9.9", version="2.9.9")
    newest = make_legacy(tmp_path / "Local_Codex_Agent_v2.10.8", version="2.10.8")
    assert discover_legacy_install(list(tmp_path.iterdir())) == newest


def test_migration_is_idempotent_and_never_modifies_source(tmp_path):
    source = make_complete_legacy(tmp_path / "legacy")
    before = snapshot_tree(source)
    first = migrate_legacy_install(
        candidates=[source], data_root=tmp_path / "data", local_config_path=tmp_path / "config.json",
        clock=lambda: 1000.0,
    )
    second = migrate_legacy_install(
        candidates=[source], data_root=tmp_path / "data", local_config_path=tmp_path / "config.json",
        clock=lambda: 2000.0,
    )
    assert first.skipped is False
    assert second.skipped is True
    assert snapshot_tree(source) == before
```

Add separate tests for absent, partial, corrupt, and disconnected-path input; deep field merge; and a recursive scan proving secret values never occur in the destination tree or receipt.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_local_codex_migration.py -q`

Expected: import failure because `migration.py` does not exist.

- [ ] **Step 3: Add tracked defaults and ignored state**

Add a `local_codex` object to `config.default.json` with `enabled`, `data_root`, worker stop/heartbeat settings, log rotation limits, legacy candidate paths, and default Tornado configuration. Add `data/local_codex/` to `.gitignore` while retaining any existing broader data exceptions.

- [ ] **Step 4: Implement category-isolated copying**

Validate each category independently. Use temporary files plus `Path.replace()` for destination writes. Import explicit legacy values over defaults, but never copy values from keys matching `password`, `secret`, `token`, `api_key`, or configured credential names. Preserve registered paths even when currently unavailable.

- [ ] **Step 5: Write the receipt last**

Only the final successful step writes `receipt.json`. Include source path, semantic version, completion timestamp, imported category names, and nonsecret warnings. Receipt existence with `status == "completed"` makes later calls return `skipped=True`.

- [ ] **Step 6: Verify and commit**

Run: `python -m pytest tests/test_local_codex_migration.py -q`

```bash
git add .gitignore config.default.json hcs_ai/local_codex/migration.py tests/test_local_codex_migration.py
git commit -m "feat: migrate standalone Local Codex state into HCS"
```

---

### Task 4: Build the Worker Process and Task Lifecycle

**Files:**
- Create: `hcs_ai/local_codex/worker.py`
- Modify: `hcs_ai/local_codex/runtime.py`
- Test: `tests/test_local_codex_worker.py`

**Interfaces:**
- Consumes: Task 1 `run_task`; Task 2 `Command`, `EventFactory`, and `EventLog`.
- Produces: `WorkerRuntime(input_stream, output_stream, *, data_root, runtime_factory, clock, heartbeat_seconds)`.
- Produces module entry point: `python -m hcs_ai.local_codex.worker --data-root PATH`.

- [ ] **Step 1: Write failing worker protocol tests**

Use `io.StringIO` plus an injected fake runtime to prove:

```python
def test_worker_rejects_second_task_as_busy_without_starting_it():
    runtime = BlockingRuntime()
    worker = WorkerRuntime(
        input_stream=io.StringIO(), output_stream=io.StringIO(),
        data_root=Path("test-data"), runtime_factory=lambda: runtime,
        clock=lambda: 1000.0, heartbeat_seconds=30.0,
    )
    worker.handle(command("submit_task", {"workspace_id": "one", "prompt": "first"}))
    worker.handle(command("submit_task", {"workspace_id": "one", "prompt": "second"}))
    assert event_types(worker)[:2] == ["task_started", "command_error"]
    assert last_event(worker).payload["code"] == "busy"
```

Add tests for submit, resume, approval routing, status, cooperative stop, shutdown, exception-to-`task_failed`, journal-preserving cancellation, heartbeat emission, malformed command, and monotonic events across task threads.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_local_codex_worker.py -q`

Expected: import failure because `worker.py` does not exist.

- [ ] **Step 3: Implement a single task thread**

The stdin loop remains responsive while one daemon task thread calls `runtime.run_task`. Store a `threading.Event` for cooperative cancellation. Serialize all output through one locked `emit()` method so sequence and NDJSON lines cannot interleave.

- [ ] **Step 4: Add task and approval state transitions**

Allowed task states are `idle`, `working`, `awaiting_approval`, `stopping`, `completed`, `blocked`, and `failed`. Reject transitions not represented in the worker state table. Approval commands must match the current request id.

- [ ] **Step 5: Verify real module protocol smoke test**

Run the module with a `status` line followed by `shutdown`; assert exit code zero and parse every stdout line as a protocol v1 event. No diagnostic prose may appear on stdout; stderr is reserved for process-level failures.

- [ ] **Step 6: Run tests and commit**

Run: `python -m pytest tests/test_local_codex_worker.py tests/local_codex -q`

```bash
git add hcs_ai/local_codex/worker.py hcs_ai/local_codex/runtime.py tests/test_local_codex_worker.py
git commit -m "feat: add supervised Local Codex worker runtime"
```

---

### Task 5: Build LocalCodexService Supervision and Recovery

**Files:**
- Create: `hcs_ai/local_codex/service.py`
- Test: `tests/test_local_codex_service.py`

**Interfaces:**
- Consumes: protocol v1 events from Task 2 and worker entry point from Task 4.
- Produces: `LocalCodexService` operations named in the spec.
- Produces: nonblocking `poll_events(limit=200) -> list[Event]` and `status() -> dict`.

- [ ] **Step 1: Write failing service tests against a fake Popen**

```python
def test_service_sends_typed_submit_and_preserves_event_order(tmp_path):
    process = FakeProcess(events=[event(1, "worker_started"), event(2, "task_started")])
    service = LocalCodexService(data_root=tmp_path, process_factory=lambda *a, **k: process)
    service.start()
    result = service.submit_task("maze", "Fix portal collision")
    assert result.accepted is True
    assert json.loads(process.stdin.lines[-1])["type"] == "submit_task"
    assert [item.sequence for item in wait_for_events(service, 2)] == [1, 2]
```

Add tests for no-shell worker launch, stdout/stderr readers, malformed lines, duplicate/out-of-order sequences, queue pressure retaining critical events, heartbeat timeout state, unexpected exit, restart, cooperative stop followed by forced tree termination, shutdown idempotence, and workspace list/refresh.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_local_codex_service.py -q`

Expected: import failure because `service.py` does not exist.

- [ ] **Step 3: Implement safe process launch and readers**

Launch an argument list with `shell=False`, `cwd=ROOT`, text mode, line buffering, stdin/stdout/stderr pipes, and Windows `CREATE_NO_WINDOW`. Reader threads parse stdout events and turn stderr into redacted warning events. Never hold the service lock during blocking I/O.

- [ ] **Step 4: Implement bounded event buffering**

Use a bounded deque. When full, evict the oldest `log` event first. Never evict lifecycle, task, approval, action result, or error events. Record one coalesced warning for dropped verbose logs.

- [ ] **Step 5: Implement stopping and crash recovery**

Send `stop_task`, wait the configured grace period, then use the existing `terminate_process_tree(pid)` behavior through an injected terminator. Unexpected exit emits `worker_crashed`; `restart_worker()` launches a fresh worker without deleting journals.

- [ ] **Step 6: Verify and commit**

Run: `python -m pytest tests/test_local_codex_service.py tests/test_local_codex_worker.py -q`

```bash
git add hcs_ai/local_codex/service.py tests/test_local_codex_service.py
git commit -m "feat: supervise Local Codex from HCS"
```

---

### Task 6: Add the Full-Control Local Codex Tab and Live Log

**Files:**
- Create: `hcs_ai/gui_local_codex.py`
- Modify: `hcs_ai/gui.py`
- Modify: `hcs_ai/gui_tree.py`
- Test: `tests/test_local_codex_gui.py`

**Interfaces:**
- Consumes: `LocalCodexService` from Task 5.
- Produces: `LOCAL_CODEX_TAB_TITLE = "Local Codex"`.
- Produces: `LocalCodexGuiMixin.build_local_codex()`, `_poll_local_codex_events()`, and pure `format_local_codex_event(event) -> (text, tag)`.
- Base `App` accepts optional `local_codex_service`; production host supplies it, tests may omit it.

- [ ] **Step 1: Write failing pure formatting and tab contract tests**

```python
def test_format_provider_fallback_uses_provider_tag():
    event = Event(
        protocol_version=1, sequence=4, timestamp=1000.0,
        type="provider_fallback", level="info",
        payload={"from_provider": "cloud", "to_provider": "local-lm-studio"},
    )
    text, tag = format_local_codex_event(event)
    assert text == "Provider fallback: cloud → local-lm-studio"
    assert tag == "provider"


def test_local_codex_tab_title_and_controls_are_declared():
    assert LOCAL_CODEX_TAB_TITLE == "Local Codex"
    assert REQUIRED_CONTROL_NAMES == {
        "workspace", "refresh", "manage", "prompt", "new_task", "resume", "stop",
        "approve", "deny", "follow", "pause_scroll", "search", "clear_view", "open_log_folder",
    }
```

Add Tk tests when a display is available; otherwise use fake widgets/service to test state transitions. Cover idle, working, awaiting approval, stopping, crashed, and completed control enablement; event ordering; search highlights; follow/pause; clear-view without file deletion; and no direct executor import.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_local_codex_gui.py -q`

Expected: import failure because `gui_local_codex.py` does not exist.

- [ ] **Step 3: Create the focused GUI mixin**

Build the workspace row, prompt editor, action buttons, state indicators, approval strip, log toolbar, and read-only `tk.Text` log. Configure tags `normal`, `action`, `success`, `warning`, `provider`, and `error`. Use the existing shared clipboard bindings.

- [ ] **Step 4: Wire nonblocking polling**

Call `service.poll_events()` only from an `after(100, ...)` callback. Render a batch in sequence order, update status fields, and scroll only when Follow is enabled and Pause Scroll is disabled. No worker thread may call a Tk method.

- [ ] **Step 5: Add the notebook tab without enlarging gui.py behavior methods**

Have the concrete app inherit the mixin and create `self.local_codex_tab` with the other notebook frames. Call `build_local_codex()` during initialization. Preserve existing tab order except placing Local Codex immediately before System.

- [ ] **Step 6: Verify and commit**

Run: `python -m pytest tests/test_local_codex_gui.py tests/test_home_tab.py tests/test_ui_status_polish.py -q`

```bash
git add hcs_ai/gui_local_codex.py hcs_ai/gui.py hcs_ai/gui_tree.py tests/test_local_codex_gui.py
git commit -m "feat: add Local Codex control and log tab"
```

---

### Task 7: Integrate DesktopHost Lifecycle, Migration, and Process Cleanup

**Files:**
- Modify: `hcs_ai/desktop_host.py`
- Modify: `hcs_ai/gui.py`
- Test: `tests/test_local_codex_desktop_host.py`

**Interfaces:**
- Consumes: migration Task 3 and service Task 5.
- `DesktopHost.local_codex` owns exactly one `LocalCodexService`.
- `App(local_codex_service: LocalCodexService | None = None)` receives the facade.

- [ ] **Step 1: Write failing host lifecycle tests**

```python
def test_hiding_window_keeps_local_codex_running(host):
    host.hide_window()
    host.local_codex.shutdown.assert_not_called()


def test_full_exit_stops_local_codex_before_hcs_server(host):
    host.exit()
    assert host.stop_order == ["local_codex", "server", "ui"]
```

Add tests for migration before service start, worker start after server readiness, restart cleanup, startup failure leaving HCS usable, shutdown idempotence, and forced termination delegated through the existing process-tree helper.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_local_codex_desktop_host.py -q`

Expected: failures because `DesktopHost` has no Local Codex service.

- [ ] **Step 3: Construct and pass the service**

Run one-time migration after HCS configuration loads. Construct the service with `ROOT / "data" / "local_codex"`, start it after the HCS server is ready, and pass it to `App`.

- [ ] **Step 4: Extend `_stop_children` in deterministic order**

Request Local Codex shutdown first, then terminate any surviving Local Codex worker tree, then stop the HCS server. Hiding to tray must call none of these.

- [ ] **Step 5: Verify and commit**

Run: `python -m pytest tests/test_local_codex_desktop_host.py tests/test_desktop_host_server_python.py tests/test_model_switch_shutdown.py -q`

```bash
git add hcs_ai/desktop_host.py hcs_ai/gui.py tests/test_local_codex_desktop_host.py
git commit -m "feat: manage Local Codex with the HCS desktop host"
```

---

### Task 8: Ship HCS 0.11.0 and Verify the Integrated Product

**Files:**
- Modify: `VERSION`
- Modify: `config.default.json`
- Modify: `hcs_ai/__init__.py`
- Modify: `update_manifest.json`
- Modify: `install.ps1`
- Modify: `README.md`
- Create: `RELEASE_NOTES_v0.11.0.txt`
- Create: `docs/LOCAL_CODEX_WINDOWS_SMOKE_TEST.md`
- Test: `tests/test_local_codex_release.py`
- Test: `tests/test_local_codex_integration.py`

**Interfaces:**
- Consumes: all previous tasks.
- Produces: HCS version `0.11.0`, complete updater manifest, installation coverage, and end-to-end worker/service flow.

- [ ] **Step 1: Write failing release contract tests**

```python
def test_release_versions_and_manifest_include_local_codex():
    assert Path("VERSION").read_text(encoding="utf-8").strip() == "0.11.0"
    assert hcs_ai.__version__ == "0.11.0"
    config = json.loads(Path("config.default.json").read_text(encoding="utf-8"))
    assert config["app"]["version"] == "0.11.0"
    manifest = json.loads(Path("update_manifest.json").read_text(encoding="utf-8"))
    paths = set(manifest_paths(manifest))
    assert "hcs_ai/local_codex/worker.py" in paths
    assert "hcs_ai/gui_local_codex.py" in paths
```

Add a manifest test that every tracked Python file under `hcs_ai/local_codex/` is shipped. Add an integration test that launches the real worker subprocess, requests status, submits a fake-runtime task through an injected test mode, observes ordered events, and shuts down cleanly.

- [ ] **Step 2: Run release tests and verify RED**

Run: `python -m pytest tests/test_local_codex_release.py tests/test_local_codex_integration.py -q`

Expected: version and manifest assertions fail at 0.10.0 and missing files.

- [ ] **Step 3: Update release metadata and updater coverage**

Set `VERSION`, `hcs_ai.__version__`, and `config.default.json` to `0.11.0`. Ensure install/update scripts copy package directories recursively or enumerate every new file. Keep mutable `data/local_codex/` and local `config.json` excluded from replacement.

- [ ] **Step 4: Document operation and rollback**

README must explain first migration, the Local Codex tab, log controls, Stop/Resume, approval behavior, tray versus full exit, and rollback to the untouched standalone folder. Release notes list the imported version and known first-release single-task limit.

- [ ] **Step 5: Write the Windows smoke-test checklist**

The checklist contains exact expected observations for startup, migration receipt, workspace selection, LM Studio task, cloud-to-local fallback, approval, Stop/Resume, crash/restart, tray hiding, full exit process cleanup, Open Log Folder, and HCS update/restart.

- [ ] **Step 6: Run focused and full verification**

Run:

```bash
python -m pytest tests/test_local_codex_release.py tests/test_local_codex_integration.py -q
python -m pytest -q
python -m compileall -q hcs_ai
git diff --check
```

Require zero failures, syntax errors, or whitespace errors.

- [ ] **Step 7: Commit**

```bash
git add VERSION config.default.json hcs_ai/__init__.py update_manifest.json install.ps1 README.md \
  RELEASE_NOTES_v0.11.0.txt docs/LOCAL_CODEX_WINDOWS_SMOKE_TEST.md \
  tests/test_local_codex_release.py tests/test_local_codex_integration.py
git commit -m "release: integrate Local Codex in HCS 0.11.0"
```
