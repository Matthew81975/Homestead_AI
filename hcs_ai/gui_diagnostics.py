from __future__ import annotations

import json
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .config import ROOT
from .diagnostics import DiagnosticEvent, TelemetrySnapshot, get_diagnostics
from . import telemetry
from . import gui as base_gui
from . import gui_tree
from . import gui_recent


DIAGNOSTICS = get_diagnostics(ROOT / "data" / "logs")


class DetailedApiError(RuntimeError):
    def __init__(self, status: int | None, reason: str, body: str = ""):
        self.status = status
        self.reason = reason
        self.body = body
        prefix = f"HTTP {status}: " if status is not None and not reason.startswith("HTTP ") else ""
        super().__init__(prefix + reason)


def concise_http_reason(status: int, body: str) -> str:
    body = (body or "").strip()
    if body:
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                detail = parsed.get("detail") or parsed.get("error") or parsed.get("message")
                if isinstance(detail, dict):
                    detail = detail.get("message") or json.dumps(detail, ensure_ascii=False)
                if detail:
                    return str(detail).strip()
        except (ValueError, TypeError):
            pass
        compact = " ".join(body.split())
        return f"HTTP {status}: {compact[:500]}"
    return f"HTTP {status}"


def _endpoint_port() -> int | None:
    try:
        return urllib.parse.urlparse(base_gui.BASE or "").port
    except ValueError:
        return None


def diagnostic_api(method, path, data=None, timeout=180):
    if not base_gui.BASE:
        DIAGNOSTICS.emit("ERROR", "HTTP", path, "HCS-AI server has not been found yet.")
        raise ConnectionError("HCS-AI server has not been found yet.")

    body, headers = None, {}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    url = base_gui.BASE + path
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    started = time.perf_counter()
    DIAGNOSTICS.update_telemetry(
        state="Busy",
        backend_state="running",
        backend_port=_endpoint_port(),
        active_subsystem="HTTP",
        active_operation=f"{method} {path}",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            elapsed = time.perf_counter() - started
            result = json.loads(raw) if raw.strip() else {}
            DIAGNOSTICS.emit(
                "DEBUG",
                "HTTP",
                path,
                f"{method} {path} completed",
                elapsed_seconds=elapsed,
                http_method=method,
                http_endpoint=path,
                http_status=getattr(response, "status", 200),
                context={"response_bytes": len(raw)},
                diagnostic_payload={"request": data, "response": result},
            )
            DIAGNOSTICS.update_telemetry(state="Ready", backend_state="running", backend_port=_endpoint_port())
            return result
    except urllib.error.HTTPError as exc:
        elapsed = time.perf_counter() - started
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""
        reason = concise_http_reason(int(exc.code), raw)
        DIAGNOSTICS.emit(
            "ERROR",
            "HTTP",
            path,
            reason,
            elapsed_seconds=elapsed,
            http_method=method,
            http_endpoint=path,
            http_status=int(exc.code),
            diagnostic_payload={"request": data, "response_body": raw},
            exception=repr(exc),
        )
        DIAGNOSTICS.update_telemetry(state="Ready", last_error=reason)
        raise DetailedApiError(int(exc.code), reason, raw) from exc
    except Exception as exc:
        elapsed = time.perf_counter() - started
        DIAGNOSTICS.emit(
            "ERROR",
            "HTTP",
            path,
            f"{method} {path} failed: {exc}",
            elapsed_seconds=elapsed,
            http_method=method,
            http_endpoint=path,
            diagnostic_payload={"request": data},
            exception=repr(exc),
        )
        DIAGNOSTICS.update_telemetry(state="Ready", last_error=str(exc))
        raise


# Existing GUI methods resolve their module-level `api` globals at runtime. Replacing
# those globals lets the diagnostics layer remain isolated from the large legacy GUI files.
base_gui.api = diagnostic_api
gui_tree.api = diagnostic_api
gui_recent.api = diagnostic_api


def format_status_text(snapshot: TelemetrySnapshot, development_mode: bool) -> str:
    parts = [snapshot.state or "Ready"]
    if snapshot.active_model:
        parts.append(f"Model: {Path(snapshot.active_model).name}")
    if snapshot.last_response_seconds is not None:
        parts.append(f"Last response: {snapshot.last_response_seconds:.2f} s")
    if snapshot.output_tokens_per_second is not None:
        parts.append(f"{snapshot.output_tokens_per_second:.1f} tok/s")
    if development_mode:
        if snapshot.backend_state or snapshot.backend_port:
            backend = snapshot.backend_state or "backend"
            if snapshot.backend_port:
                backend += f":{snapshot.backend_port}"
            parts.append(backend)
        if snapshot.prompt_tokens_per_second is not None:
            parts.append(f"prompt {snapshot.prompt_tokens_per_second:.1f} tok/s")
        if snapshot.last_http_status is not None:
            parts.append(f"HTTP {snapshot.last_http_status}")
        if snapshot.active_subsystem:
            operation = f".{snapshot.active_operation}" if snapshot.active_operation else ""
            parts.append(f"{snapshot.active_subsystem}{operation}")
        if snapshot.diagnostic_payload_capture:
            parts.append("payloads ON")
        if snapshot.last_error:
            parts.append(f"last error: {snapshot.last_error}")
    return " | ".join(parts)


class App(gui_recent.App):
    """Final HCS GUI layer providing development-only observability."""

    def __init__(self):
        super().__init__()
        self.title("HCS-AI 0.10.0")
        self.diagnostics = DIAGNOSTICS
        self._notebook = next(
            (child for child in self.winfo_children() if isinstance(child, ttk.Notebook)),
            None,
        )
        if self._notebook is None:
            raise RuntimeError("HCS top-level notebook was not found.")

        self.log_tab = ttk.Frame(self._notebook)
        self._notebook.add(self.log_tab, text="Log")
        self._build_log_tab()

        self.developer_status = ttk.Label(self, anchor="w", relief="sunken")
        self.developer_status.pack(side="bottom", fill="x")
        self.after(350, self._refresh_diagnostics_ui)

    def send(self):
        """Run one chat request while preserving diagnostic controls and measuring wall time."""
        if not self.prompt.get().strip():
            return

        development_mode = bool(self.dev_mode_var.get())
        payload_capture = bool(self.payload_var.get())
        self.diagnostics.development_mode = development_mode
        self.diagnostics.capture_diagnostic_payloads = payload_capture

        started = time.perf_counter()
        self.diagnostics.update_telemetry(
            state="Busy",
            active_subsystem="Chat",
            active_operation="request",
        )
        self.diagnostics.emit("INFO", "Chat", "request", "Chat request started")

        try:
            super().send()
        finally:
            elapsed = time.perf_counter() - started

            # The request path must never change these user-controlled diagnostics flags.
            self.diagnostics.development_mode = development_mode
            self.diagnostics.capture_diagnostic_payloads = payload_capture
            self.dev_mode_var.set(development_mode)
            self.payload_var.set(payload_capture)

            self.diagnostics.update_telemetry(
                state="Ready",
                last_response_seconds=elapsed,
                diagnostic_payload_capture=payload_capture,
                active_subsystem="Chat",
                active_operation="complete",
            )
            self.diagnostics.emit(
                "INFO",
                "Chat",
                "complete",
                f"Chat request finished in {elapsed:.3f}s wall time",
                elapsed_seconds=elapsed,
                context={"wall_clock_seconds": elapsed},
            )
            self._refresh_status()

    def _build_log_tab(self):
        controls = ttk.Frame(self.log_tab)
        controls.pack(fill="x", padx=8, pady=8)

        self.dev_mode_var = tk.BooleanVar(value=self.diagnostics.development_mode)
        self.payload_var = tk.BooleanVar(value=self.diagnostics.capture_diagnostic_payloads)
        self.autoscroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            controls,
            text="Development Diagnostics",
            variable=self.dev_mode_var,
            command=self._toggle_development_mode,
        ).pack(side="left")
        ttk.Checkbutton(
            controls,
            text="Capture Diagnostic Payloads",
            variable=self.payload_var,
            command=self._toggle_payload_capture,
        ).pack(side="left", padx=(10, 0))
        ttk.Checkbutton(controls, text="Auto-scroll", variable=self.autoscroll_var).pack(side="left", padx=(10, 0))

        filters = ttk.Frame(self.log_tab)
        filters.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(filters, text="Severity:").pack(side="left")
        self.log_severity = tk.StringVar(value="ALL")
        severity = ttk.Combobox(filters, textvariable=self.log_severity, values=("ALL", "DEBUG", "INFO", "WARNING", "ERROR"), width=10, state="readonly")
        severity.pack(side="left", padx=(4, 10))
        severity.bind("<<ComboboxSelected>>", lambda _e: self._render_log())

        ttk.Label(filters, text="Subsystem:").pack(side="left")
        self.log_subsystem = tk.StringVar(value="ALL")
        self.subsystem_combo = ttk.Combobox(filters, textvariable=self.log_subsystem, values=("ALL",), width=18, state="readonly")
        self.subsystem_combo.pack(side="left", padx=(4, 10))
        self.subsystem_combo.bind("<<ComboboxSelected>>", lambda _e: self._render_log())

        ttk.Label(filters, text="Search:").pack(side="left")
        self.log_search = tk.StringVar()
        search = ttk.Entry(filters, textvariable=self.log_search)
        search.pack(side="left", fill="x", expand=True, padx=4)
        search.bind("<KeyRelease>", lambda _e: self._render_log())

        actions = ttk.Frame(self.log_tab)
        actions.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(actions, text="Copy Selected", command=self._copy_selected_log).pack(side="left")
        ttk.Button(actions, text="Copy Visible", command=self._copy_visible_log).pack(side="left", padx=4)
        ttk.Button(actions, text="Export", command=self._export_log).pack(side="left", padx=4)
        ttk.Button(actions, text="Clear", command=self._clear_log).pack(side="left", padx=4)

        pane = ttk.Panedwindow(self.log_tab, orient="vertical")
        pane.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        upper = ttk.Frame(pane)
        lower = ttk.Frame(pane)
        pane.add(upper, weight=3)
        pane.add(lower, weight=2)

        cols = ("time", "severity", "subsystem", "operation", "message")
        self.log_tree = ttk.Treeview(upper, columns=cols, show="headings", selectmode="browse")
        for col, title, width in (
            ("time", "Time", 165),
            ("severity", "Severity", 70),
            ("subsystem", "Subsystem", 110),
            ("operation", "Operation", 160),
            ("message", "Message", 480),
        ):
            self.log_tree.heading(col, text=title)
            self.log_tree.column(col, width=width)
        scroll = ttk.Scrollbar(upper, orient="vertical", command=self.log_tree.yview)
        self.log_tree.configure(yscrollcommand=scroll.set)
        self.log_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.log_tree.bind("<<TreeviewSelect>>", self._show_log_detail)

        self.log_detail = tk.Text(lower, wrap="word", state="disabled", height=10)
        detail_scroll = ttk.Scrollbar(lower, orient="vertical", command=self.log_detail.yview)
        self.log_detail.configure(yscrollcommand=detail_scroll.set)
        self.log_detail.pack(side="left", fill="both", expand=True)
        detail_scroll.pack(side="right", fill="y")
        self._log_records: dict[str, DiagnosticEvent] = {}
        self._last_event_count = -1

    def _toggle_development_mode(self):
        self.diagnostics.development_mode = bool(self.dev_mode_var.get())
        self.diagnostics.emit(
            "INFO",
            "Diagnostics",
            "mode",
            f"Development Diagnostics {'enabled' if self.diagnostics.development_mode else 'disabled'}",
        )
        self._refresh_status()

    def _toggle_payload_capture(self):
        self.diagnostics.capture_diagnostic_payloads = bool(self.payload_var.get())
        self.diagnostics.update_telemetry(diagnostic_payload_capture=self.diagnostics.capture_diagnostic_payloads)
        self.diagnostics.emit(
            "WARNING" if self.diagnostics.capture_diagnostic_payloads else "INFO",
            "Diagnostics",
            "payload_capture",
            f"Diagnostic payload capture {'enabled' if self.diagnostics.capture_diagnostic_payloads else 'disabled'}",
        )
        self._refresh_status()

    def _visible_events(self) -> list[DiagnosticEvent]:
        severity = self.log_severity.get()
        severities = None if severity == "ALL" else [severity]
        subsystem = self.log_subsystem.get()
        subsystem = None if subsystem == "ALL" else subsystem
        return self.diagnostics.events(severities=severities, subsystem=subsystem, search=self.log_search.get())

    def _render_log(self):
        events = self._visible_events()
        self.log_tree.delete(*self.log_tree.get_children())
        self._log_records = {}
        for event in events:
            iid = f"e{event.event_id}"
            self._log_records[iid] = event
            timestamp = event.timestamp.replace("T", " ")[:23]
            self.log_tree.insert(
                "",
                "end",
                iid=iid,
                values=(timestamp, event.severity, event.subsystem, event.operation, event.message),
            )
        if events and self.autoscroll_var.get():
            self.log_tree.see(f"e{events[-1].event_id}")

        subsystems = sorted({e.subsystem for e in self.diagnostics.events()})
        self.subsystem_combo.configure(values=("ALL", *subsystems))
        if self.log_subsystem.get() not in ("ALL", *subsystems):
            self.log_subsystem.set("ALL")

    def _show_log_detail(self, _event=None):
        selected = self.log_tree.selection()
        if not selected:
            return
        event = self._log_records.get(selected[0])
        if not event:
            return
        payload = self.diagnostics.event_dict(event, include_payloads=self.diagnostics.capture_diagnostic_payloads)
        text = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        self.log_detail.config(state="normal")
        self.log_detail.delete("1.0", "end")
        self.log_detail.insert("1.0", text)
        self.log_detail.config(state="disabled")

    def _copy_selected_log(self):
        selected = self.log_tree.selection()
        if not selected:
            return
        event = self._log_records.get(selected[0])
        if not event:
            return
        text = json.dumps(self.diagnostics.event_dict(event, include_payloads=self.diagnostics.capture_diagnostic_payloads), indent=2, ensure_ascii=False)
        self.clipboard_clear()
        self.clipboard_append(text)

    def _copy_visible_log(self):
        lines = [
            f"{e.timestamp} {e.severity} {e.subsystem}.{e.operation}: {e.message}"
            for e in self._visible_events()
        ]
        self.clipboard_clear()
        self.clipboard_append("\n".join(lines))

    def _export_log(self):
        path = filedialog.asksaveasfilename(
            title="Export HCS log",
            defaultextension=".log",
            filetypes=(("Human-readable log", "*.log"), ("Text", "*.txt"), ("JSON Lines", "*.jsonl")),
        )
        if not path:
            return
        try:
            self.diagnostics.export(
                path,
                events=self._visible_events(),
                include_payloads=self.diagnostics.capture_diagnostic_payloads,
            )
            self.diagnostics.emit("INFO", "Diagnostics", "export", f"Exported log to {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("Export log", str(exc))

    def _clear_log(self):
        self.diagnostics.clear()
        self._render_log()

    def _refresh_status(self):
        try:
            latest = telemetry.latest()
            if latest:
                self.diagnostics.update_telemetry(
                    active_model=latest.get("model"),
                    prompt_tokens_per_second=latest.get("prompt_tokens_per_second"),
                    output_tokens_per_second=latest.get("generation_tokens_per_second"),
                    backend_state="running" if base_gui.BASE else "disconnected",
                    backend_port=_endpoint_port(),
                )
            else:
                self.diagnostics.update_telemetry(
                    backend_state="running" if base_gui.BASE else "disconnected",
                    backend_port=_endpoint_port(),
                )
        except Exception:
            pass
        snapshot = self.diagnostics.telemetry()
        self.developer_status.config(text=format_status_text(snapshot, self.diagnostics.development_mode))

    def _refresh_diagnostics_ui(self):
        try:
            event_count = len(self.diagnostics.events())
            if event_count != self._last_event_count:
                self._last_event_count = event_count
                self._render_log()
            self._refresh_status()
        finally:
            self.after(700, self._refresh_diagnostics_ui)

    def _focus_log(self, search: str = ""):
        self._notebook.select(self.log_tab)
        if search:
            self.log_search.set(search)
        self._render_log()

    def _classification_dialog(self, message: str, errors: list[dict]):
        dialog = tk.Toplevel(self)
        dialog.title("Knowledge Base")
        dialog.transient(self)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=message, justify="left", wraplength=620).pack(anchor="w")
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="OK", command=dialog.destroy).pack(side="right")
        if errors:
            def view_log():
                dialog.destroy()
                self._focus_log("classification")
            ttk.Button(buttons, text="View Log", command=view_log).pack(side="right", padx=(0, 8))
        dialog.wait_window()

    def _import(self, path):
        if not path:
            return
        started = time.perf_counter()
        self.diagnostics.emit(
            "INFO",
            "KnowledgeBase",
            "import",
            f"Import started: {Path(path).name}",
            diagnostic_payload={"path": path},
        )
        try:
            out = diagnostic_api("POST", "/knowledge/import", {"path": path})
            classified = out.get("artifacts_classified", 0)
            errors = out.get("classification_errors") or []
            elapsed = time.perf_counter() - started
            self.diagnostics.emit(
                "INFO" if not errors else "WARNING",
                "KnowledgeBase",
                "classification",
                f"Imported {out.get('files_imported', 0)} file(s); classified {classified}; errors {len(errors)}",
                elapsed_seconds=elapsed,
                context={"files": out.get("files_imported", 0), "chunks": out.get("chunks", 0), "classified": classified, "errors": len(errors)},
                diagnostic_payload={"result": out},
            )
            for error in errors:
                self.diagnostics.emit(
                    "ERROR",
                    "KnowledgeBase",
                    "classification",
                    str(error.get("error") or "Artifact classification failed"),
                    diagnostic_payload=error,
                )
            msg = (
                f"Imported {out.get('files_imported', 0)} files / {out.get('chunks', 0)} chunks.\n"
                f"LLM classified {classified} artifact(s) into the Knowledge Tree."
            )
            if errors:
                first_reason = str(errors[0].get("error") or "Unknown classification error")
                msg += f"\n{len(errors)} artifact(s) could not be classified.\nReason: {first_reason}"
            self._classification_dialog(msg, errors)
            self.refresh_knowledge_tree()
        except Exception as exc:
            self.diagnostics.emit("ERROR", "KnowledgeBase", "import", str(exc), exception=repr(exc))
            messagebox.showerror("Knowledge Base", str(exc))
