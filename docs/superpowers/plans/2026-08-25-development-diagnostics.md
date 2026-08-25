# Development Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an exportable Log tab, shared application diagnostics/telemetry, detailed HTTP/model-load errors, Knowledge Base classifier diagnostics, and a runtime-toggleable development HUD.

**Architecture:** Introduce a UI-independent diagnostics service that records structured events, persists rotating JSONL session logs, filters/redacts exports, and maintains the latest telemetry snapshot. Existing LLM/model/Knowledge Base code emits events into that service; Tkinter GUI consumes the service for the Log tab and bottom status/diagnostics strip.

**Tech Stack:** Python 3.10+, Tkinter/ttk, dataclasses, json, pathlib, threading-safe primitives, existing urllib/HTTP and HCS telemetry code, pytest/unittest patterns already in repository.

**Spec:** `docs/superpowers/specs/2026-08-25-development-diagnostics-design.md`

## Global Constraints

- Development Diagnostics defaults off.
- Diagnostic payload capture defaults off independently.
- Keep approximately 10 session log files.
- Logging failures must never crash HCS.
- Reuse existing model telemetry rather than creating conflicting timing sources.
- No live model or internet dependency in unit tests.

---

### Task 1: Shared diagnostics service

**Files:**
- Create: `hcs_ai/diagnostics.py`
- Create/modify: repository diagnostics unit test file under the existing test layout.

**Interfaces:**
- Produces `DiagnosticEvent`, `TelemetrySnapshot`, and `DiagnosticsService`.
- `DiagnosticsService.emit(...) -> DiagnosticEvent`
- `DiagnosticsService.events(...) -> list[DiagnosticEvent]`
- `DiagnosticsService.update_telemetry(...) -> TelemetrySnapshot`
- `DiagnosticsService.export(path, events, include_payloads=False)`

- [ ] Write failing tests for event creation, filtering, redaction, JSONL/text serialization, telemetry updates, and 10-session rotation.
- [ ] Run those tests and verify they fail because the service does not exist.
- [ ] Implement the minimal diagnostics service and redaction helpers.
- [ ] Run the diagnostics tests and verify green.
- [ ] Commit `feat: add shared diagnostics service`.

### Task 2: HTTP/model-load error diagnostics

**Files:**
- Modify: `hcs_ai/model_manager.py`
- Modify: `hcs_ai/llm.py` and/or the existing HTTP client helper actually used by model switching.
- Test: existing model manager/LLM test files.

**Interfaces:**
- Consumes `DiagnosticsService.emit`.
- Produces a helper/result that preserves HTTP status and response body and derives a concise user-facing reason.

- [ ] Write a failing test where a fake HTTP 400 body contains a useful backend reason and assert the raised/displayable error includes it.
- [ ] Verify RED.
- [ ] Implement response-body capture, concise extraction, and diagnostics emission with request/response details placed in diagnostic payloads.
- [ ] Verify GREEN and existing model tests.
- [ ] Commit `fix: expose backend model load errors`.

### Task 3: Knowledge Base classification instrumentation

**Files:**
- Modify: `hcs_ai/knowledge_tree.py`
- Modify: `hcs_ai/knowledge.py` if classification is orchestrated there.
- Test: Knowledge Tree/Knowledge Base tests.

**Interfaces:**
- Emits `KnowledgeBase` events for ingest/classify stages, success path, parsed result, and traceback on failure.
- Returns/propagates a concise classification failure reason and event identifier for the GUI.

- [ ] Write failing tests for successful classifier telemetry and classifier parse/failure logging.
- [ ] Verify RED.
- [ ] Add minimal instrumentation and preserve raw prompt/response only in diagnostic payloads.
- [ ] Verify GREEN.
- [ ] Commit `feat: instrument knowledge classification`.

### Task 4: Log tab and export UI

**Files:**
- Modify: `hcs_ai/gui.py`
- Create: `hcs_ai/gui_log.py` if consistent with the existing `gui_home.py`/`gui_recent.py` split.
- Test: GUI formatting/filter helper tests where available.

**Interfaces:**
- Consumes `DiagnosticsService.events`, export, and mode flags.
- Produces top-level Log tab with severity/subsystem/search filters, auto-scroll, clear/copy/export controls, Development Diagnostics toggle, and Diagnostic Payload Capture toggle.

- [ ] Write failing tests for pure filter/format helpers or tab controller behavior that can run headlessly.
- [ ] Verify RED.
- [ ] Implement Log tab and export dialogs.
- [ ] Verify GREEN and application import/startup tests.
- [ ] Commit `feat: add exportable log tab`.

### Task 5: Status bar and developer HUD

**Files:**
- Modify: `hcs_ai/gui.py`
- Modify: existing telemetry plumbing in `hcs_ai/gui_recent.py`, `hcs_ai/engine.py`, or `hcs_ai/llm.py` as required by actual code ownership.
- Test: formatting/state tests.

**Interfaces:**
- Reads `TelemetrySnapshot`.
- Normal status bar shows readiness/model/last response time and tok/s where available.
- Development mode expands with backend port, HTTP status, subsystem operation, last error, and payload-capture indicator.

- [ ] Write failing tests for normal vs. diagnostics status formatting.
- [ ] Verify RED.
- [ ] Implement runtime-toggleable status/HUD updates using one telemetry snapshot.
- [ ] Verify GREEN.
- [ ] Commit `feat: add development diagnostics hud`.

### Task 6: GUI error linkage and regression verification

**Files:**
- Modify: `hcs_ai/gui.py` and Knowledge Base/Models tab handlers.
- Modify: updater/release metadata files if new runtime files must be shipped.
- Test: regression suite.

**Interfaces:**
- Model-load errors show concise backend reason.
- Knowledge Base classification dialog can open/focus the Log tab around the corresponding error event.

- [ ] Add failing regression tests for the two observed bugs: generic HTTP 400 and opaque classification failure.
- [ ] Verify RED.
- [ ] Wire dialogs to concise reasons and View Log behavior.
- [ ] Verify GREEN.
- [ ] Run full test suite and startup/import smoke checks available in CI.
- [ ] Update release notes/updater manifests so new diagnostics files ship.
- [ ] Commit `feat: integrate diagnostics workflow`.

### Task 7: Final verification

- [ ] Run the complete automated test suite.
- [ ] Inspect diff for accidental raw secret/prompt persistence and verify payload capture defaults off.
- [ ] Verify imports compile and GUI startup path references all newly shipped files.
- [ ] Create/update pull request with testing instructions: launch HCS, enable Development Diagnostics, reproduce Qwen3-0.6B model load, inspect exact 400 body in Log, then re-import the Knowledge Base test corpus and inspect classifier events.
