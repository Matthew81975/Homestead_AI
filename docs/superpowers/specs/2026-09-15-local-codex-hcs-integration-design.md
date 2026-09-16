# Local Codex HCS Integration Design

## Goal

Make Local Codex 2.10.8 a native HCS subsystem and provide a full-control Local Codex tab with live, structured logs. HCS becomes the normal launcher, supervisor, configuration owner, and updater while preserving Local Codex's task journal, workspace safety, provider routing, and approval boundaries.

## Scope

This release imports the Local Codex runtime, adds an HCS-owned worker boundary, creates the GUI tab, and performs a one-time migration from a standalone installation. It does not yet turn email into a shared HCS-wide messaging service or unify Tornado with HCS's separate cloud router. Those integrations remain later focused phases.

The target HCS release is `0.11.0` because this adds a new managed subsystem and user-facing workflow.

## Chosen Architecture

Local Codex becomes the `hcs_ai.local_codex` package. Its task engine runs in a child worker process launched and owned by HCS. The code and update lifecycle are native to the HCS repository, while the process boundary prevents long model or network calls from blocking Tkinter and allows HCS to terminate and restart a stuck worker.

The GUI never imports and invokes the action executor directly. It talks to a `LocalCodexService` facade, which validates typed commands, manages the worker, and publishes typed events. The worker retains Local Codex's existing controller and action-executor authority checks.

## Components

### Imported runtime

- Move the Local Codex 2.10.8 `agent` modules beneath `hcs_ai/local_codex/` and convert imports to package-relative imports.
- Preserve Tornado provider routing, including immediate local fallback before provider waits.
- Preserve task journals, workspace registry, prompt optimizer, Git workflow, email worker, self-update compatibility helpers, and existing action schemas where they remain applicable.
- Retain a temporary terminal entry point for recovery and focused testing. HCS is the normal interface after migration.
- HCS's updater owns future delivery of the integrated package. The imported Local Codex updater must not independently replace HCS files.

### Service facade

Create `hcs_ai/local_codex/service.py` with a `LocalCodexService` that exposes typed operations:

- `start()` and `shutdown()` for worker lifecycle.
- `list_workspaces()` and workspace registry refresh.
- `submit_task(workspace_id, prompt)` for a new task.
- `resume_task(workspace_id)` for the persisted interrupted task.
- `stop_task()` for cooperative cancellation followed by bounded forced termination if necessary.
- `approve(request_id, approved)` for pending action approvals.
- `restart_worker()` after worker failure.
- `poll_events(limit)` for nonblocking GUI consumption.
- `status()` for a current snapshot.

Only one task executes at a time in the first release. The service rejects a second start request with an explicit `busy` result rather than silently queueing it.

### Worker protocol

Create an HCS-launched module entry point, `python -m hcs_ai.local_codex.worker`. Use newline-delimited JSON over standard input and output so the worker remains testable without a platform-specific IPC dependency.

Commands contain `protocol_version`, `command_id`, `type`, and a validated payload. Events contain `protocol_version`, `sequence`, `timestamp`, `type`, `level`, and a redacted payload. Supported event families include:

- lifecycle: `worker_started`, `worker_stopping`, `worker_stopped`, `worker_crashed`;
- task: `task_started`, `task_resumed`, `task_stopping`, `task_completed`, `task_blocked`, `task_failed`;
- routing: `provider_selected`, `provider_fallback`, `provider_wait`, `provider_recovered`;
- actions: `action_started`, `action_result`, `approval_required`, `approval_resolved`;
- output: `log`, `test_result`, and `status_snapshot`.

Unknown commands receive a structured error event. Malformed worker output is recorded as a protocol warning and cannot be interpreted as a command. Sequence numbers let the GUI preserve ordering even when it polls in batches.

## Local Codex Tab

Add a `Local Codex` frame to the existing top Tkinter notebook and build it in a focused `hcs_ai/gui_local_codex.py` mixin rather than increasing the already-large `gui.py`.

The tab contains:

- a registered-workspace selector with refresh and workspace-management access;
- a multiline prompt editor;
- `New Task`, `Resume`, `Stop`, and context-sensitive approval controls;
- worker state, task state, active provider, and elapsed-time indicators;
- a scrollable, read-only live log with selectable text;
- `Follow`, `Pause Scroll`, `Search`, `Clear View`, and `Open Log Folder` controls.

Clearing the view never deletes persistent logs. Log rows use restrained tags for normal output, actions, success, warnings, provider state, and errors. The GUI polls the service event queue with `after(...)`; worker threads and processes never modify Tk widgets directly.

Approval requests appear inside the tab with a concise action summary and explicit Approve/Deny controls. Existing commit, push, deletion, credential, and other sensitive-action rules remain authoritative. Routine actions continue to follow Local Codex configuration.

## Lifecycle and Recovery

`DesktopHost` owns the Local Codex service alongside the HCS server process:

- HCS startup creates the service and starts the idle worker after the main window is ready.
- Closing the window to the tray leaves the worker and any active task running.
- `Exit HCS` requests graceful task cancellation, waits a bounded interval, then terminates the worker process tree before exiting.
- `Restart HCS` performs the same cleanup before relaunch.
- Unexpected worker exit creates a `worker_crashed` event with the exit code and last safe diagnostic line.
- The task journal is flushed at action boundaries. After a crash, `Restart Worker` followed by `Resume` continues the same persisted task rather than creating a duplicate.
- A worker heartbeat detects a nonresponsive process. HCS reports the condition but does not kill a task until the user presses Stop or exits/restarts HCS.

## One-Time Migration

On first startup of HCS 0.11.0, a migration component searches configured legacy paths, the known standalone installation location, and sibling `Local_Codex_Agent_v*` directories. It selects the highest semantic version containing a valid Local Codex configuration.

The migrator imports compatible workspace registrations, Tornado configuration, email settings, task queues, task journals, and safe preferences. It merges field-by-field: explicit legacy choices win, while current HCS defaults supply missing fields. Paths are normalized but not required to exist during migration, so disconnected drives do not erase registrations.

Secrets remain in their current environment variables, OS-managed stores, or ignored local credential files. They are never placed in tracked HCS configuration, the migration receipt, worker events, or ordinary logs.

Migration writes a local receipt containing source path, source version, completion time, imported categories, and nonsecret warnings. The receipt makes migration idempotent. HCS becomes authoritative afterward; it does not continue reading live configuration from the standalone folder. The old folder is not modified, moved, or deleted and remains a rollback copy.

If no valid installation is found, HCS creates clean Local Codex defaults and the tab remains usable. A partial or corrupt source imports only independently valid categories and reports the rest without preventing HCS startup.

## Configuration and Storage

Tracked defaults live under `config.default.json`; local overrides remain in the ignored `config.json`. Mutable Local Codex state lives beneath `ROOT/data/local_codex/`, separated into `workspaces`, `tasks`, `logs`, `tornado`, and `migration` directories. Existing configuration loaders remain backward compatible with HCS 0.10.0 files.

Provider credentials stay referenced by environment-variable name. Persistent logs use rotation and a configurable retention limit. UI `Clear View` affects only the widget. The service redacts bearer tokens, passwords, known credential values, prompt content marked sensitive, and environment values associated with configured secret names before publishing or writing events.

## Error Handling

- Invalid GUI commands return typed validation errors and never reach the executor.
- Unregistered or out-of-bound workspace paths are rejected by the existing workspace boundary.
- Worker startup failure leaves HCS operational and exposes `Restart Worker` plus a safe diagnostic.
- Broken JSON lines, unknown event versions, and out-of-order sequence numbers produce warnings without executing data.
- A full event queue drops only verbose log events under pressure; lifecycle, task, approval, action-result, and error events are retained.
- Stop first requests cooperative cancellation. After the configured grace period, HCS may terminate the worker process tree while preserving the journal.
- Log and migration errors degrade to visible warnings; they do not crash the HCS GUI.

## Testing

Port the Local Codex test suite to the new package namespace and keep its behavioral coverage. Add tests for:

- configuration migration from complete, partial, corrupt, and absent legacy installations;
- idempotent migration and preservation of the standalone source;
- secret exclusion from tracked configuration, receipts, events, and logs;
- typed protocol validation, ordering, malformed input, and version mismatch;
- worker startup, cooperative stop, forced termination, crash detection, heartbeat status, restart, and task resume;
- rejection of unregistered and out-of-bound workspace paths;
- single-task busy behavior and approval command routing;
- GUI construction, control enablement, event rendering, search, follow/pause behavior, and clear-view persistence semantics;
- Tkinter responsiveness while the worker or model is blocked;
- HCS tray hiding versus full exit/restart process cleanup;
- existing HCS tests and the imported Local Codex tests in the same test run.

Manual Windows smoke testing covers HCS startup, one-time migration, workspace selection, a short LM Studio task, provider fallback, approval, Stop/Resume, tray hiding, full exit, log-folder access, and update/restart behavior.

## Release and Rollback

Release as HCS `0.11.0`. Update the manifest, installer/update scripts, README, and release notes so all imported package files and GUI/service modules ship through the existing updater.

Rollback consists of closing HCS 0.11.0 and launching the untouched standalone Local Codex installation. Because migration copies rather than mutates legacy state, rollback requires no reverse migration.

## Deferred Work

- Elevate email and other remote-control channels into a shared HCS messaging service used by Local Codex, HKR, Alexandria, and future agents.
- Consolidate Tornado and HCS cloud-provider routing behind a shared provider service.
- Add concurrent task workers after single-task lifecycle and recovery are proven stable.
- Replace the temporary terminal compatibility entry point once the HCS interface has demonstrated reliable recovery in normal use.
