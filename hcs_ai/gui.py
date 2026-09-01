import json
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox, simpledialog
import urllib.request, urllib.parse
import os
import re
import subprocess
import threading
import uuid
from pathlib import Path
from .config import ROOT, load_config, update_local_config
from .speech import SpeechEngine, download_natural_voice_assets, natural_voice_ready
from .ports import port_candidates, saved_endpoint

BASE = None


def markdown_segments(text):
    """Split a small, safe Markdown subset for display in Tk text widgets."""
    value = str(text or "")
    segments = []
    position = 0
    for match in re.finditer(r"\*\*(.+?)\*\*", value, flags=re.DOTALL):
        if match.start() > position:
            segments.append((value[position:match.start()], None))
        segments.append((match.group(1), "bold"))
        position = match.end()
    if position < len(value):
        segments.append((value[position:], None))
    return segments or [(value, None)]


def read_update_state(path):
    """Read updater state written by either Windows PowerShell or PowerShell 7."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _health_at(host, port, timeout=0.25):
    base = f"http://{host}:{port}"
    with urllib.request.urlopen(base + "/health", timeout=timeout) as response:
        health = json.loads(response.read().decode("utf-8"))
    if health.get("name") != "HCS-AI":
        raise RuntimeError("The service on this port is not HCS-AI.")
    return base, health


def discover_server():
    endpoints = []
    saved = saved_endpoint()
    if saved:
        endpoints.append(saved)
    endpoints.extend(("127.0.0.1", port) for port in port_candidates())
    seen = set()
    for endpoint in endpoints:
        if endpoint in seen:
            continue
        seen.add(endpoint)
        try:
            return _health_at(*endpoint)
        except Exception:
            pass
    return None, None

def api(method, path, data=None, timeout=180):
    if not BASE:
        raise ConnectionError("HCS-AI server has not been found yet.")
    body, headers = None, {}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("HCS-AI v0.7.1 — Self-Contained Local AI")
        self.geometry("1050x720")
        self.history = []
        self._thinking = False
        self._thinking_frame = 0
        self.cloud_task_id = "alexandria-" + uuid.uuid4().hex
        self._pending_cloud_request = None
        self._speech_engine = SpeechEngine()
        self._install_clipboard_bindings()

        # Main workspace: persistent tabbed tools above, Alexandria console below.
        # The AI is the command interface to HCS, not a destination tab.
        self.workspace_pane = ttk.Panedwindow(self, orient="vertical")
        self.workspace_pane.pack(fill="both", expand=True)

        self.tabs = ttk.Notebook(self.workspace_pane)
        self.chat_tab = ttk.Frame(self.workspace_pane)
        self.kb_tab, self.hkr_tab, self.ext_tab, self.mem_tab, self.mcp_tab, self.sys_tab = [
            ttk.Frame(self.tabs) for _ in range(6)
        ]
        for frame, title in zip(
            [self.kb_tab, self.hkr_tab, self.ext_tab, self.mem_tab, self.mcp_tab, self.sys_tab],
            ["Knowledge Base", "HKR Librarian", "External Library", "Memory", "MCP", "System"],
        ):
            self.tabs.add(frame, text=title)

        self.workspace_pane.add(self.tabs, weight=4)
        self.workspace_pane.add(self.chat_tab, weight=1)

        self.build_chat()
        self.build_kb(); self.build_hkr(); self.build_external(); self.build_memory(); self.build_mcp(); self.build_system()
        self.after(80, self._set_ai_console_normal)
        self.after(300, self.check_server)
        self.after(1200, self._refresh_git_update_status)

    def _install_clipboard_bindings(self):
        # Shared clipboard behavior for HCS text controls. This includes
        # read-only/disabled Text widgets, which Tk does not handle consistently.
        for widget_class in ("Text", "Entry", "TEntry", "Spinbox", "TSpinbox"):
            self.bind_class(widget_class, "<Control-c>", self._clipboard_copy, add="+")
            self.bind_class(widget_class, "<Control-C>", self._clipboard_copy, add="+")
            self.bind_class(widget_class, "<Control-a>", self._clipboard_select_all, add="+")
            self.bind_class(widget_class, "<Control-A>", self._clipboard_select_all, add="+")
            self.bind_class(widget_class, "<Button-3>", self._show_clipboard_menu, add="+")
        for widget_class in ("Text", "Entry", "TEntry", "Spinbox", "TSpinbox"):
            self.bind_class(widget_class, "<Control-x>", self._clipboard_cut, add="+")
            self.bind_class(widget_class, "<Control-X>", self._clipboard_cut, add="+")
            self.bind_class(widget_class, "<Control-v>", self._clipboard_paste, add="+")
            self.bind_class(widget_class, "<Control-V>", self._clipboard_paste, add="+")

    def _widget_editable(self, widget):
        try:
            return str(widget.cget("state")) not in ("disabled", "readonly")
        except Exception:
            return True

    def _selected_text(self, widget):
        try:
            if isinstance(widget, tk.Text):
                return widget.get("sel.first", "sel.last")
            return widget.selection_get()
        except Exception:
            return ""

    def _clipboard_copy(self, event):
        text = self._selected_text(event.widget)
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update_idletasks()
        return "break"

    def _clipboard_cut(self, event):
        widget = event.widget
        if not self._widget_editable(widget):
            return self._clipboard_copy(event)
        text = self._selected_text(widget)
        if not text:
            return "break"
        self.clipboard_clear()
        self.clipboard_append(text)
        try:
            if isinstance(widget, tk.Text):
                widget.delete("sel.first", "sel.last")
            else:
                first, last = widget.index("sel.first"), widget.index("sel.last")
                widget.delete(first, last)
        except Exception:
            pass
        return "break"

    def _clipboard_paste(self, event):
        widget = event.widget
        if not self._widget_editable(widget):
            return "break"
        try:
            text = self.clipboard_get()
        except tk.TclError:
            return "break"
        try:
            if isinstance(widget, tk.Text):
                try:
                    widget.delete("sel.first", "sel.last")
                except tk.TclError:
                    pass
                widget.insert("insert", text)
            else:
                try:
                    first, last = widget.index("sel.first"), widget.index("sel.last")
                    widget.delete(first, last)
                except Exception:
                    pass
                widget.insert("insert", text)
        except Exception:
            pass
        return "break"

    def _clipboard_select_all(self, event):
        widget = event.widget
        try:
            if isinstance(widget, tk.Text):
                widget.tag_add("sel", "1.0", "end-1c")
                widget.mark_set("insert", "1.0")
                widget.see("insert")
            else:
                widget.selection_range(0, "end")
                widget.icursor("end")
        except Exception:
            pass
        return "break"

    def _show_clipboard_menu(self, event):
        widget = event.widget
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Copy", command=lambda: self._clipboard_copy(type("E", (), {"widget": widget})()))
        editable = self._widget_editable(widget)
        menu.add_command(label="Cut", state="normal" if editable else "disabled",
                         command=lambda: self._clipboard_cut(type("E", (), {"widget": widget})()))
        menu.add_command(label="Paste", state="normal" if editable else "disabled",
                         command=lambda: self._clipboard_paste(type("E", (), {"widget": widget})()))
        menu.add_separator()
        menu.add_command(label="Select All", command=lambda: self._clipboard_select_all(type("E", (), {"widget": widget})()))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def check_server(self):
        global BASE
        try:
            BASE, h = discover_server()
            if not BASE:
                raise ConnectionError
            self.status.config(text=f"Connected — {h['name']} {h['version']} at {BASE}")
        except Exception:
            self.status.config(text="Looking for HCS-AI server...")
            self.after(1000, self.check_server)

    def build_chat(self):
        header = ttk.Frame(self.chat_tab)
        header.pack(fill="x", padx=8, pady=(6, 0))
        ttk.Label(header, text="Alexandria — HCS AI").pack(side="left")
        ttk.Label(header, text="Mode:").pack(side="left", padx=(12, 2))
        self.ai_mode_var = tk.StringVar(value="offline")
        self.offline_mode_button = ttk.Radiobutton(
            header, text="Offline", variable=self.ai_mode_var, value="offline",
            command=lambda: self._set_ai_mode("offline"),
        )
        self.offline_mode_button.pack(side="left")
        self.live_mode_button = ttk.Radiobutton(
            header, text="Live", variable=self.ai_mode_var, value="live",
            command=lambda: self._set_ai_mode("live"),
        )
        self.live_mode_button.pack(side="left", padx=(2, 8))
        self.live_availability = ttk.Label(
            header, text="Internet: checking... | Cloud AI: checking..."
        )
        self.live_availability.pack(side="left", padx=(2, 8))
        ttk.Button(
            header,
            text="Cloud Models",
            command=self.show_cloud_models,
        ).pack(side="left", padx=(2, 8))
        self.cloud_route_label = ttk.Label(header, text="Cloud: —")
        self.cloud_route_label.pack(side="left", padx=(4, 8))
        self.thinking_label = ttk.Label(header, text="")
        self.thinking_label.pack(side="left", padx=(4, 8))
        self.git_update_label = ttk.Label(header, text="Git: checking...")
        self.git_update_label.pack(side="right", padx=(8, 8))
        ttk.Button(header, text="Expand", command=self._expand_ai_console).pack(side="right")
        ttk.Button(header, text="Normal", command=self._set_ai_console_normal).pack(side="right", padx=4)
        ttk.Button(header, text="Collapse", command=self._collapse_ai_console).pack(side="right")

        chat_area = ttk.Frame(self.chat_tab)
        chat_area.pack(fill="both", expand=True, padx=8, pady=6)
        self.chat_box = tk.Text(chat_area, wrap="word", state="disabled", height=8)
        base_chat_font = tkfont.Font(font=self.chat_box.cget("font"))
        self._chat_bold_font = base_chat_font.copy()
        self._chat_bold_font.configure(weight="bold")
        self.chat_box.tag_configure("chat_bold", font=self._chat_bold_font)
        chat_scroll = ttk.Scrollbar(chat_area, orient="vertical", command=self.chat_box.yview)
        self.chat_box.configure(yscrollcommand=chat_scroll.set)
        self.chat_box.pack(side="left", fill="both", expand=True)
        chat_scroll.pack(side="right", fill="y")

        row = ttk.Frame(self.chat_tab); row.pack(fill="x", padx=8, pady=(0,6))
        self.prompt = ttk.Entry(row); self.prompt.pack(side="left", fill="x", expand=True)
        self.prompt.bind("<Return>", lambda e: self.send())
        self.send_button = ttk.Button(row, text="Send", command=self.send)
        self.send_button.pack(side="left", padx=(8,0))
        self.use_kb = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="Use KB", variable=self.use_kb).pack(side="left", padx=8)
        voice_settings = load_config().get("voice", {})
        self.speak_replies = tk.BooleanVar(
            value=bool(voice_settings.get("speak_replies", False))
        )
        self.voice_toggle = ttk.Checkbutton(
            row,
            text="Speak replies",
            variable=self.speak_replies,
            command=self._set_speak_replies,
        )
        self.voice_toggle.pack(side="left", padx=(0, 4))
        self.voice_setup_button = ttk.Button(
            row,
            text="Voice Setup",
            command=self._setup_natural_voice,
        )
        self.voice_setup_button.pack(side="left", padx=(0, 8))
        self.status = ttk.Label(self.chat_tab, text="Checking server...")
        self.after(1500, self._refresh_ai_mode_status)
        self.status.pack(anchor="w", padx=8, pady=(0,6))

    def show_cloud_models(self):
        window = tk.Toplevel(self)
        window.title("Cloud Model Pool")
        window.geometry("900x480")

        ttk.Label(
            window,
            text="Cloud Model Pool — models grouped by capability tier and provider",
        ).pack(anchor="w", padx=10, pady=(10, 4))

        frame = ttk.Frame(window)
        frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        columns = ("providers", "routes", "failover", "state")
        tree = ttk.Treeview(frame, columns=columns, show="tree headings")
        self.cloud_models_tree = tree
        tree.heading("#0", text="Tier / Model / Provider")
        tree.heading("providers", text="Providers")
        tree.heading("routes", text="Healthy / Configured")
        tree.heading("failover", text="Auto failover")
        tree.heading("state", text="State / Credential")
        tree.column("#0", width=280, stretch=True)
        tree.column("providers", width=190, stretch=True)
        tree.column("routes", width=120, anchor="center")
        tree.column("failover", width=110, anchor="center")
        tree.column("state", width=170, stretch=True)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        loading = tree.insert("", "end", text="Loading cloud models...")

        def work():
            try:
                out = api(
                    "GET",
                    "/ai/models?task_id=" + urllib.parse.quote(self.cloud_task_id),
                    timeout=12,
                )
                self.after(
                    0,
                    lambda data=out, t=tree: self._populate_cloud_models(t, data),
                )
            except Exception as exc:
                self.after(
                    0,
                    lambda message=str(exc), t=tree, item=loading: (
                        t.item(item, text="Unable to load cloud models"),
                        t.set(item, "state", message),
                    ),
                )

        threading.Thread(target=work, daemon=True).start()

    def _populate_cloud_models(self, tree, data):
        for item in tree.get_children():
            tree.delete(item)

        for tier_info in data.get("tiers", []):
            tier_name = str(tier_info.get("tier") or "unassigned")
            tier_item = tree.insert(
                "",
                "end",
                text=tier_name.upper(),
                open=True,
            )
            for model_info in tier_info.get("models", []):
                providers = model_info.get("providers", [])
                provider_names = ", ".join(
                    str(p.get("provider") or "")
                    for p in providers
                    if p.get("provider")
                )
                active = bool(model_info.get("active"))
                model_name = str(model_info.get("model") or "")
                display_name = ("★ " if active else "") + model_name
                healthy = int(model_info.get("healthy_routes", 0))
                configured = int(model_info.get("configured_routes", 0))
                model_state = "Active" if active else (
                    "Ready" if healthy else "Unavailable"
                )
                model_item = tree.insert(
                    tier_item,
                    "end",
                    text=display_name,
                    values=(
                        provider_names,
                        f"{healthy} / {configured}",
                        "Yes" if model_info.get("same_tier_failover_eligible") else "No",
                        model_state,
                    ),
                    open=active,
                )
                for provider in providers:
                    key_state = (
                        "Key: Ready"
                        if provider.get("credential_configured")
                        else "Key: Missing"
                    )
                    route_state = str(provider.get("state") or "unknown").title()
                    tree.insert(
                        model_item,
                        "end",
                        text=str(provider.get("provider") or "provider"),
                        values=(
                            "",
                            "1 / 1" if provider.get("healthy") else "0 / 1",
                            "",
                            f"{route_state} | {key_state}",
                        ),
                    )

    def _set_ai_mode(self, mode: str):
        previous = getattr(self, "_last_ai_mode", "offline")
        try:
            out = api("POST", "/ai/mode", {"mode": mode}, timeout=10)
            self._apply_ai_mode_status(out)
        except Exception as exc:
            self.ai_mode_var.set(previous)
            messagebox.showerror("AI mode", str(exc))

    def _apply_ai_mode_status(self, out: dict):
        selected = str(out.get("selected_mode") or "offline")
        effective = str(out.get("effective_mode") or "offline")
        self._last_ai_mode = selected
        self._effective_ai_mode = effective
        self.ai_mode_var.set(selected)
        internet = bool(out.get("internet_available"))
        cloud = bool(out.get("cloud_configured"))
        live = bool(out.get("live_available"))
        self.live_mode_button.configure(state="normal" if live else "disabled")
        internet_text = "Available" if internet else "Unavailable"
        cloud_text = "Ready" if cloud else "Not configured"
        source = "Cloud" if effective == "live" else "Local"
        self.live_availability.config(
            text=f"Internet: {internet_text} | Cloud AI: {cloud_text} | AI: {source}"
        )
        if effective != "live":
            self.cloud_route_label.config(text="Cloud: —")
        elif not self.cloud_route_label.cget("text").startswith("Cloud: ") or self.cloud_route_label.cget("text") == "Cloud: —":
            pool = out.get("cloud_pool") or {}
            healthy = pool.get("healthy_routes", 0)
            configured = pool.get("configured_routes", 0)
            self.cloud_route_label.config(
                text=f"Cloud: ready | {healthy}/{configured} routes"
            )

    def _refresh_ai_mode_status(self):
        try:
            if BASE:
                self._apply_ai_mode_status(api("GET", "/ai/status", timeout=8))
        except Exception:
            self.live_mode_button.configure(state="disabled")
            self.live_availability.config(
                text="Internet: unknown | Cloud AI: unavailable | AI: Local"
            )
        finally:
            self.after(10000, self._refresh_ai_mode_status)

    def _set_ai_sash(self, bottom_height: int):
        try:
            self.update_idletasks()
            total = max(1, self.workspace_pane.winfo_height())
            top_height = max(60, total - max(44, int(bottom_height)))
            self.workspace_pane.sashpos(0, top_height)
        except tk.TclError:
            pass

    def _collapse_ai_console(self):
        self._set_ai_sash(46)

    def _set_ai_console_normal(self):
        self.update_idletasks()
        total = max(1, self.workspace_pane.winfo_height())
        self._set_ai_sash(max(190, int(total * 0.30)))

    def _expand_ai_console(self):
        self.update_idletasks()
        total = max(1, self.workspace_pane.winfo_height())
        self._set_ai_sash(max(260, int(total * 0.78)))

    def _setup_natural_voice(self):
        if natural_voice_ready():
            self._speech_engine = SpeechEngine()
            messagebox.showinfo("Natural Voice", "Alexandria's natural voice is ready.")
            return
        approved = messagebox.askyesno(
            "Install Natural Voice",
            "Download Alexandria's approximately 354 MB Kokoro voice pack? "
            "It runs offline after installation and has no usage fees.",
        )
        if not approved:
            return
        self.voice_setup_button.configure(state="disabled")
        self.status.config(text="Downloading Alexandria's natural voice...")

        def work():
            try:
                download_natural_voice_assets()
                self.after(0, self._natural_voice_setup_done)
            except Exception as exc:
                self.after(0, lambda m=str(exc): self._natural_voice_setup_failed(m))

        threading.Thread(target=work, daemon=True).start()

    def _natural_voice_setup_done(self):
        self._speech_engine = SpeechEngine()
        self.voice_setup_button.configure(state="normal")
        self.status.config(text="Natural voice ready")
        messagebox.showinfo(
            "Natural Voice",
            "Alexandria's warm conversational voice is installed and ready.",
        )

    def _natural_voice_setup_failed(self, message):
        self.voice_setup_button.configure(state="normal")
        self.status.config(text="Natural voice setup failed; standard voice remains available")
        messagebox.showerror("Natural Voice Setup", message)

    def _set_speak_replies(self):
        enabled = bool(self.speak_replies.get())
        if enabled and not self._speech_engine.available:
            self.speak_replies.set(False)
            messagebox.showwarning(
                "Voice unavailable",
                "No local text-to-speech engine was found on this computer.",
            )
            return
        try:
            update_local_config({"voice": {"speak_replies": enabled}})
        except OSError as exc:
            self.speak_replies.set(not enabled)
            messagebox.showerror("Voice setting", f"Could not save voice setting: {exc}")

    def append_chat(self, who, text):
        self.chat_box.config(state="normal")
        self.chat_box.insert("end", f"{who}:\n")
        for segment, style in markdown_segments(text):
            tags = ("chat_bold",) if style == "bold" else ()
            self.chat_box.insert("end", segment, tags)
        self.chat_box.insert("end", "\n\n")
        self.chat_box.see("end")
        self.chat_box.config(state="disabled")
        if (
            who == "HCS-AI"
            and hasattr(self, "speak_replies")
            and self.speak_replies.get()
        ):
            self._speech_engine.speak(text)

    def _set_thinking(self, active):
        self._thinking = bool(active)
        self._thinking_frame = 0
        if active:
            self.send_button.configure(state="disabled")
            self.prompt.configure(state="disabled")
            self._animate_thinking()
        else:
            self.thinking_label.config(text="")
            self.send_button.configure(state="normal")
            self.prompt.configure(state="normal")
            self.prompt.focus_set()

    def _animate_thinking(self):
        if not self._thinking:
            return
        frames = ("Thinking ·", "Thinking ··", "Thinking ···")
        self.thinking_label.config(text=frames[self._thinking_frame % len(frames)])
        self._thinking_frame += 1
        self.after(320, self._animate_thinking)

    def send(self):
        msg = self.prompt.get().strip()
        if not msg or self._thinking:
            return
        payload = {
            "message": msg,
            "history": list(self.history[-12:]),
            "use_kb": bool(self.use_kb.get()),
            "task_id": self.cloud_task_id,
        }
        self._pending_cloud_request = payload
        self.prompt.delete(0, "end")
        self.append_chat("You", msg)
        self._set_thinking(True)
        self._dispatch_chat_payload(payload)

    def _dispatch_chat_payload(self, payload):
        def work():
            try:
                out = api("POST", "/chat", payload)
                self.after(
                    0,
                    lambda o=out, m=payload["message"]: self._chat_done(m, o),
                )
            except Exception as exc:
                self.after(0, lambda m=str(exc): self._chat_failed(m))

        threading.Thread(target=work, daemon=True).start()

    def _retry_pending_cloud_request(self):
        payload = self._pending_cloud_request
        if not payload:
            return
        self._set_thinking(True)
        self._dispatch_chat_payload(payload)

    def _chat_done(self, msg, out):
        if out.get("approval_required"):
            self._set_thinking(False)
            approved = messagebox.askyesno(
                "Cloud model change",
                out.get("message") or (
                    f"Continue by changing capability tier from "
                    f"{out.get('current_tier')} to {out.get('proposed_tier')}?"
                ),
            )
            if approved:
                try:
                    api(
                        "POST",
                        "/ai/approve-tier",
                        {
                            "task_id": self.cloud_task_id,
                            "tier": out["proposed_tier"],
                        },
                        timeout=10,
                    )
                except Exception as exc:
                    self._pending_cloud_request = None
                    self.append_chat("Error", str(exc))
                    return
                self._retry_pending_cloud_request()
            else:
                self._pending_cloud_request = None
                self.append_chat(
                    "HCS-AI",
                    "Cloud task paused. Model caliber was not changed.",
                )
            return

        self._set_thinking(False)
        if out.get("provider") and out.get("model"):
            self.cloud_route_label.config(
                text=(
                    f"Cloud: {out.get('tier', '?')} | "
                    f"{out['provider']} | {out['model']}"
                )
            )
        text = out.get("text", "")
        self.append_chat("HCS-AI", text)
        self.history += [
            {"role": "user", "content": msg},
            {"role": "assistant", "content": text},
        ]
        self._pending_cloud_request = None
        if out.get("tool_results"):
            self.append_chat(
                "Tools",
                json.dumps(out["tool_results"], indent=2)[:5000],
            )

    def _chat_failed(self, message):
        self._set_thinking(False)
        self._pending_cloud_request = None
        self.append_chat("Error", message)

    def _refresh_git_update_status(self):
        if not hasattr(self, "git_update_label"):
            return
        self.git_update_label.config(text="Git: checking...")

        def work():
            state_path = ROOT / ".hcs-update" / "state.json"
            state = read_update_state(state_path)
            installed = str(state.get("installed_sha") or "").lower() or None

            latest = None
            try:
                req = urllib.request.Request(
                    "https://github.com/Matthew81975/Homestead_AI/commits/main.atom",
                    headers={"User-Agent": "HCS-AI-Update-Indicator"},
                )
                with urllib.request.urlopen(req, timeout=12) as response:
                    feed = response.read().decode("utf-8", errors="replace")
                match = re.search(r"/commit/([0-9a-fA-F]{40})", feed)
                if not match:
                    match = re.search(r"([0-9a-fA-F]{40})", feed)
                if match:
                    latest = match.group(1).lower()
            except Exception:
                pass

            if latest and installed:
                result = "Git: current" if latest == installed else "Git: update available"
            elif latest:
                result = "Git: update status unknown"
            else:
                result = "Git: offline/unavailable"
            self.after(0, lambda value=result: self.git_update_label.config(text=value))

        threading.Thread(target=work, daemon=True).start()
        self.after(600000, self._refresh_git_update_status)

    def build_kb(self):
        top = ttk.Frame(self.kb_tab); top.pack(fill="x", padx=8, pady=8)
        ttk.Button(top,text="Import File",command=self.import_file).pack(side="left")
        ttk.Button(top,text="Import Folder",command=self.import_folder).pack(side="left",padx=8)
        self.kb_query=ttk.Entry(top); self.kb_query.pack(side="left",fill="x",expand=True,padx=8)
        ttk.Button(top,text="Search",command=self.search_kb).pack(side="left")
        self.kb_results=tk.Text(self.kb_tab,wrap="word"); self.kb_results.pack(fill="both",expand=True,padx=8,pady=(0,8))

    def _import(self,path):
        if not path:return
        try:
            out=api("POST","/knowledge/import",{"path":path})
            messagebox.showinfo("Knowledge Base",f"Imported {out['files_imported']} files / {out['chunks']} chunks.")
        except Exception as e: messagebox.showerror("Knowledge Base",str(e))

    def import_file(self): self._import(filedialog.askopenfilename())
    def import_folder(self): self._import(filedialog.askdirectory())

    def search_kb(self):
        q=self.kb_query.get().strip()
        if not q:return
        try:
            out=api("GET","/knowledge/search?q="+urllib.parse.quote(q))
            self.kb_results.delete("1.0","end"); self.kb_results.insert("end",json.dumps(out,indent=2))
        except Exception as e: messagebox.showerror("Search",str(e))

    def build_hkr(self):
        from pathlib import Path
        default_root = str(Path.home() / "Documents" / "Homestead_Knowledge_Repository")

        top = ttk.Frame(self.hkr_tab); top.pack(fill="x", padx=8, pady=8)
        ttk.Label(top, text="Repository:").pack(side="left")
        self.hkr_root = tk.StringVar(value=default_root)
        ttk.Entry(top, textvariable=self.hkr_root).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(top, text="Browse", command=self.choose_hkr_root).pack(side="left")
        ttk.Button(top, text="Refresh All", command=self.refresh_hkr_all).pack(side="left", padx=(8,0))

        work = ttk.Notebook(self.hkr_tab); work.pack(fill="both", expand=True, padx=8, pady=(0,8))
        research_tab, alg_tab, software_tab = ttk.Frame(work), ttk.Frame(work), ttk.Frame(work)
        work.add(research_tab, text="Research")
        work.add(alg_tab, text="Algorithms")
        work.add(software_tab, text="Software")

        prompt_frame = ttk.LabelFrame(research_tab, text="HKR Librarian — what knowledge should I collect?")
        prompt_frame.pack(fill="x", padx=6, pady=6)
        self.hkr_prompt = tk.Text(prompt_frame, height=4, wrap="word")
        self.hkr_prompt.pack(fill="x", padx=8, pady=8)
        opts = ttk.Frame(prompt_frame); opts.pack(fill="x", padx=8, pady=(0,8))
        ttk.Label(opts, text="New docs:").pack(side="left")
        self.hkr_max = tk.IntVar(value=20)
        ttk.Spinbox(opts, from_=1, to=200, width=6, textvariable=self.hkr_max).pack(side="left", padx=(4,12))
        ttk.Label(opts, text="Results/query:").pack(side="left")
        self.hkr_depth = tk.IntVar(value=12)
        ttk.Spinbox(opts, from_=5, to=50, width=6, textvariable=self.hkr_depth).pack(side="left", padx=(4,12))
        ttk.Label(opts, text="ZIP max GB:").pack(side="left")
        self.hkr_gb = tk.DoubleVar(value=28.0)
        ttk.Spinbox(opts, from_=1, to=500, width=7, textvariable=self.hkr_gb).pack(side="left", padx=(4,12))
        self.hkr_collect_btn = ttk.Button(opts, text="Research & Add", command=self.collect_hkr)
        self.hkr_collect_btn.pack(side="right")
        ttk.Button(opts, text="Build ZIP Volumes", command=self.build_hkr_volumes).pack(side="right", padx=8)
        self.hkr_status = ttk.Label(research_tab, text="HKR status not loaded")
        self.hkr_status.pack(anchor="w", padx=8, pady=(2,6))
        self.hkr_results = tk.Text(research_tab, wrap="word")
        self.hkr_results.pack(fill="both", expand=True, padx=6, pady=(0,6))

        # Algorithm Library: searchable canonical records + provenance + code generation.
        searchbar=ttk.Frame(alg_tab); searchbar.pack(fill="x", padx=6, pady=6)
        ttk.Label(searchbar,text="Find by goal/problem:").pack(side="left")
        self.alg_query=tk.StringVar()
        ae=ttk.Entry(searchbar,textvariable=self.alg_query,width=44); ae.pack(side="left",fill="x",expand=True,padx=6)
        ae.bind("<Return>",lambda e:self.refresh_algorithms())
        ttk.Label(searchbar,text="Domain:").pack(side="left",padx=(8,2))
        self.alg_domain=tk.StringVar()
        ttk.Entry(searchbar,textvariable=self.alg_domain,width=16).pack(side="left")
        ttk.Button(searchbar,text="Search",command=self.refresh_algorithms).pack(side="left",padx=6)
        ttk.Button(searchbar,text="Scan HKR Documents",command=self.scan_algorithms).pack(side="left")

        self.alg_status=ttk.Label(alg_tab,text="Algorithm database not loaded")
        self.alg_status.pack(anchor="w",padx=8,pady=(0,4))
        pane=ttk.Panedwindow(alg_tab,orient="horizontal"); pane.pack(fill="both",expand=True,padx=6,pady=(0,6))
        left=ttk.Frame(pane); right=ttk.Frame(pane); pane.add(left,weight=3); pane.add(right,weight=2)
        cols=("name","domain","problem","time","confidence")
        self.alg_tree=ttk.Treeview(left,columns=cols,show="headings",selectmode="extended")
        for col,title in (("name","Algorithm"),("domain","Domain"),("problem","Problem class"),("time","Time"),("confidence","Confidence")):
            self.alg_tree.heading(col,text=title)
        self.alg_tree.column("name",width=210); self.alg_tree.column("domain",width=120)
        self.alg_tree.column("problem",width=190); self.alg_tree.column("time",width=100)
        self.alg_tree.column("confidence",width=80,anchor="center",stretch=False)
        asb=ttk.Scrollbar(left,orient="vertical",command=self.alg_tree.yview); self.alg_tree.configure(yscrollcommand=asb.set)
        self.alg_tree.pack(side="left",fill="both",expand=True); asb.pack(side="right",fill="y")
        self.alg_tree.bind("<<TreeviewSelect>>",self.show_algorithm_detail)

        toolbar=ttk.Frame(right); toolbar.pack(fill="x",pady=(0,4))
        ttk.Button(toolbar,text="Generate Python",command=lambda:self.generate_algorithm_code("python")).pack(side="left")
        ttk.Button(toolbar,text="Generate C++",command=lambda:self.generate_algorithm_code("cpp")).pack(side="left",padx=4)
        ttk.Button(toolbar,text="Compare Selected",command=self.compare_algorithms).pack(side="left")
        self.alg_detail=tk.Text(right,wrap="word",state="disabled")
        dsb=ttk.Scrollbar(right,orient="vertical",command=self.alg_detail.yview); self.alg_detail.configure(yscrollcommand=dsb.set)
        self.alg_detail.pack(side="left",fill="both",expand=True); dsb.pack(side="right",fill="y")
        self.alg_records={}

        software = ttk.LabelFrame(software_tab, text="Python packages (PyPI) — cached, never auto-installed")
        software.pack(fill="x", padx=6, pady=6)
        swrow = ttk.Frame(software); swrow.pack(fill="x", padx=8, pady=8)
        ttk.Label(swrow, text="Package:").pack(side="left")
        self.hkr_package = tk.StringVar(value="tensorflow")
        ttk.Entry(swrow, textvariable=self.hkr_package, width=32).pack(side="left", padx=8)
        ttk.Button(swrow, text="Find", command=self.find_python_package).pack(side="left")
        ttk.Button(swrow, text="Cache Package", command=self.cache_python_package).pack(side="left", padx=8)
        ttk.Button(swrow, text="Cache + Dependencies", command=self.cache_python_bundle).pack(side="left")
        ttk.Button(swrow, text="Show Cached", command=self.show_cached_software).pack(side="left", padx=8)
        ttk.Label(software_tab,text="Software results are displayed in the Research output panel to preserve the existing HKR workflow.").pack(anchor="w",padx=8,pady=8)
        self.after(700, self.refresh_algorithms)

    def refresh_hkr_all(self):
        self.refresh_hkr(); self.refresh_algorithms()

    def refresh_algorithms(self):
        if not hasattr(self,"alg_tree"): return
        root=self.hkr_root.get().strip(); q=self.alg_query.get().strip(); domain=self.alg_domain.get().strip()
        try:
            url="/hkr/algorithms?limit=2000&root_path="+urllib.parse.quote(root)+"&q="+urllib.parse.quote(q)+"&domain="+urllib.parse.quote(domain)
            out=api("GET",url)
            self.alg_records={d["algorithm_id"]:d for d in out}
            self.alg_tree.delete(*self.alg_tree.get_children())
            for d in out:
                self.alg_tree.insert("","end",iid=d["algorithm_id"],values=(d.get("canonical_name") or "",d.get("domain") or "",d.get("problem_class") or "",d.get("time_complexity") or "",f"{float(d.get('confidence') or 0):.0%}"))
            self.alg_status.config(text=f"{len(out)} algorithm(s) shown — search by name, goal, problem, constraints, or alternative")
        except Exception as e:
            self.alg_status.config(text="Unable to load algorithm database")
            messagebox.showerror("Algorithm Library",str(e))

    def scan_algorithms(self):
        self.alg_status.config(text="Scanning HKR documents with the local LLM...")
        import threading
        def work():
            try:
                out=api("POST","/hkr/algorithms/scan",{"root_path":self.hkr_root.get().strip(),"limit":500},timeout=3600)
                self.after(0,lambda o=out:self._scan_algorithms_done(o))
            except Exception as e:
                self.after(0,lambda m=str(e):messagebox.showerror("Algorithm scan",m))
        threading.Thread(target=work,daemon=True).start()

    def _scan_algorithms_done(self,out):
        self.refresh_algorithms()
        messagebox.showinfo("Algorithm scan",f"Scanned {out.get('documents_scanned',0)} document(s); linked {out.get('extractions',0)} extraction(s).")

    def _selected_algorithm_ids(self):
        return list(self.alg_tree.selection()) if hasattr(self,"alg_tree") else []

    def show_algorithm_detail(self,event=None):
        ids=self._selected_algorithm_ids()
        if not ids: return
        aid=ids[0]
        try:
            d=api("GET","/hkr/algorithms/"+urllib.parse.quote(aid)+"?root_path="+urllib.parse.quote(self.hkr_root.get().strip()))
        except Exception as e:
            messagebox.showerror("Algorithm",str(e)); return
        def bullets(v): return "\n".join("  • "+str(x) for x in (v or [])) or "  —"
        src="\n".join(f"  • {x.get('source_title') or x.get('object_id')}\n    {x.get('source_url') or ''}" for x in d.get("sources") or []) or "  —"
        text=(f"{d.get('canonical_name','')}\n"
              f"ID: {d.get('algorithm_id')}   Confidence: {float(d.get('confidence') or 0):.0%}   Status: {d.get('verification_status')}\n"
              f"Domain: {d.get('domain') or '—'}   Problem class: {d.get('problem_class') or '—'}\n\n"
              f"SOLVES\n{d.get('problem_solved') or '—'}\n\nDESCRIPTION\n{d.get('description') or '—'}\n\n"
              f"INPUTS\n{bullets(d.get('inputs'))}\n\nOUTPUTS\n{bullets(d.get('outputs'))}\n\n"
              f"COMPLEXITY\n  Time: {d.get('time_complexity') or 'unknown'}\n  Space: {d.get('space_complexity') or 'unknown'}\n\n"
              f"ASSUMPTIONS\n{bullets(d.get('assumptions'))}\n\nCONSTRAINTS\n{bullets(d.get('constraints'))}\n\n"
              f"FAILURE MODES\n{bullets(d.get('failure_modes'))}\n\nALTERNATIVES\n{bullets(d.get('alternatives'))}\n\n"
              f"HARDWARE / NUMERICAL NOTES\n{d.get('hardware_notes') or '—'}\n\nPSEUDOCODE\n{d.get('pseudocode') or '—'}\n\n"
              f"SOURCES\n{src}")
        self.alg_detail.config(state="normal"); self.alg_detail.delete("1.0","end"); self.alg_detail.insert("end",text); self.alg_detail.config(state="disabled")

    def generate_algorithm_code(self,language="python"):
        ids=self._selected_algorithm_ids()
        if not ids:
            messagebox.showinfo("Generate code","Select an algorithm first."); return
        requirements=simpledialog.askstring("Deployment requirements","Optional constraints (hardware, library limits, performance, interface):",parent=self) or ""
        try:
            out=api("POST","/hkr/algorithms/code",{"root_path":self.hkr_root.get().strip(),"algorithm_id":ids[0],"language":language,"requirements":requirements},timeout=600)
            win=tk.Toplevel(self); win.title(f"Generated {language} — {ids[0]}"); win.geometry("900x650")
            txt=tk.Text(win,wrap="none"); txt.pack(fill="both",expand=True,padx=8,pady=8)
            txt.insert("end",(out.get("code") or "")+"\n\nNOTES\n"+(out.get("notes") or "")+"\n\nTESTS\n"+"\n".join("- "+str(x) for x in out.get("tests") or []))
        except Exception as e: messagebox.showerror("Generate code",str(e))

    def compare_algorithms(self):
        ids=self._selected_algorithm_ids()
        if len(ids)<2:
            messagebox.showinfo("Compare algorithms","Select at least two algorithms."); return
        rows=[]
        for aid in ids[:8]:
            d=self.alg_records.get(aid) or {}
            rows.append(f"{d.get('canonical_name')}\n  Domain: {d.get('domain') or '—'}\n  Solves: {d.get('problem_solved') or '—'}\n  Time: {d.get('time_complexity') or 'unknown'} | Space: {d.get('space_complexity') or 'unknown'}\n  Constraints: {', '.join(d.get('constraints') or []) or '—'}\n  Confidence: {float(d.get('confidence') or 0):.0%}\n")
        win=tk.Toplevel(self); win.title("Algorithm comparison"); win.geometry("850x600")
        txt=tk.Text(win,wrap="word"); txt.pack(fill="both",expand=True,padx=8,pady=8); txt.insert("end","\n".join(rows)); txt.config(state="disabled")

    def find_python_package(self):
        name=self.hkr_package.get().strip()
        if not name: return
        try:
            out=api("GET", "/hkr/software/python/info?package_name="+urllib.parse.quote(name))
            self.hkr_results.delete("1.0","end")
            self.hkr_results.insert("end", json.dumps(out, indent=2))
        except Exception as e: messagebox.showerror("Python Package", str(e))

    def cache_python_package(self):
        name=self.hkr_package.get().strip()
        if not name: return
        try:
            out=api("POST", "/hkr/software/python/cache", {
                "root_path": self.hkr_root.get().strip(), "package_name": name, "prefer_binary": True
            }, timeout=1800)
            self.hkr_results.delete("1.0","end")
            self.hkr_results.insert("end", json.dumps(out, indent=2))
            messagebox.showinfo("HKR Software", f"Cached {out.get('name')} {out.get('version')}. Package was NOT installed.")
        except Exception as e: messagebox.showerror("HKR Software", str(e))

    def cache_python_bundle(self):
        name=self.hkr_package.get().strip()
        if not name: return
        try:
            out=api("POST", "/hkr/software/python/cache-bundle", {
                "root_path": self.hkr_root.get().strip(), "package_name": name, "prefer_binary": True
            }, timeout=1800)
            self.hkr_results.delete("1.0","end")
            self.hkr_results.insert("end", json.dumps(out, indent=2))
            messagebox.showinfo("HKR Software", f"Cached {out.get('count',0)} package file(s), including dependencies. Nothing was installed.")
        except Exception as e: messagebox.showerror("HKR Software", str(e))

    def show_cached_software(self):
        try:
            out=api("GET", "/hkr/software?limit=500&root_path="+urllib.parse.quote(self.hkr_root.get().strip()))
            self.hkr_results.delete("1.0","end")
            if not out:
                self.hkr_results.insert("end", "No software packages cached yet.")
                return
            for d in out:
                self.hkr_results.insert("end", f"{d.get('package_name')} {d.get('version')} — {d.get('filename')}\n")
                self.hkr_results.insert("end", f"  {d.get('summary') or ''}\n")
                self.hkr_results.insert("end", f"  Compatible wheel: {'YES' if d.get('compatible') else 'NO / source archive'}\n")
                self.hkr_results.insert("end", f"  {d.get('bytes',0)/1_000_000:.1f} MB — SHA256 {d.get('sha256','')[:16]}...\n\n")
        except Exception as e: messagebox.showerror("HKR Software", str(e))

    def choose_hkr_root(self):
        p = filedialog.askdirectory(initialdir=self.hkr_root.get() or None)
        if p:
            self.hkr_root.set(p); self.refresh_hkr()

    def refresh_hkr(self):
        root = self.hkr_root.get().strip()
        try:
            st = api("GET", "/hkr/status?root_path=" + urllib.parse.quote(root))
            docs = api("GET", "/hkr/documents?limit=100&root_path=" + urllib.parse.quote(root))
            self.hkr_status.config(text=f"{st['documents']} documents — {st['bytes']/1_000_000:.1f} MB — {st['root']}")
            self.hkr_results.delete("1.0","end")
            for d in docs:
                self.hkr_results.insert("end", f"{d.get('original_title') or d.get('filename')}\n")
                if d.get('summary'): self.hkr_results.insert("end", f"  {d['summary']}\n")
                chapters=d.get('chapter_titles') or []
                if chapters: self.hkr_results.insert("end", "  Sections: " + "; ".join(chapters[:8]) + "\n")
                if d.get('index_pointer'): self.hkr_results.insert("end", f"  Index: {d['index_pointer']}\n")
                self.hkr_results.insert("end", "\n")
        except Exception as e:
            messagebox.showerror("HKR", str(e))

    def collect_hkr(self):
        prompt = self.hkr_prompt.get("1.0","end").strip()
        if not prompt: return
        self.hkr_collect_btn.config(state="disabled")
        self.hkr_status.config(text="HKR is researching and cataloging...")
        import threading
        def work():
            try:
                out = api("POST", "/hkr/collect", {
                    "root_path": self.hkr_root.get().strip(), "prompt": prompt,
                    "max_docs": self.hkr_max.get(), "per_query": self.hkr_depth.get()
                }, timeout=1800)
                self.after(0, lambda: self._hkr_done(out))
            except Exception as e:
                msg = str(e)
                self.after(0, lambda m=msg: self._hkr_error(m))
        threading.Thread(target=work, daemon=True).start()

    def _hkr_done(self, out):
        self.hkr_collect_btn.config(state="normal")
        self.hkr_status.config(text=f"Added {out.get('saved',0)} docs to the HKR master library. Select active files in External Library.")
        self.refresh_external()
        self.hkr_results.delete("1.0","end"); self.hkr_results.insert("end", "\n".join(out.get("log") or []))

    def _hkr_error(self, msg):
        self.hkr_collect_btn.config(state="normal")
        self.hkr_status.config(text="HKR research failed")
        messagebox.showerror("HKR", msg)

    def build_hkr_volumes(self):
        try:
            out=api("POST","/hkr/volumes",{"root_path":self.hkr_root.get().strip(),"max_gb":self.hkr_gb.get()})
            self.hkr_results.delete("1.0","end"); self.hkr_results.insert("end","\n".join(out.get("log") or []))
            messagebox.showinfo("HKR",f"Built {out.get('volumes',0)} volume(s).")
        except Exception as e: messagebox.showerror("HKR",str(e))


    def build_external(self):
        top = ttk.Frame(self.ext_tab); top.pack(fill="x", padx=8, pady=8)
        ttk.Label(top, text="HKR repository:").pack(side="left")
        ttk.Entry(top, textvariable=self.hkr_root).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(top, text="Refresh", command=self.refresh_external).pack(side="left")

        actions = ttk.Frame(self.ext_tab); actions.pack(fill="x", padx=8, pady=(0,8))
        ttk.Button(actions, text="Add Selected to HCS", command=self.external_add_selected).pack(side="left")
        ttk.Button(actions, text="Remove Selected from HCS", command=self.external_remove_selected).pack(side="left", padx=8)
        ttk.Label(actions, text="Filter:").pack(side="left", padx=(16,4))
        self.ext_filter = tk.StringVar()
        ent = ttk.Entry(actions, textvariable=self.ext_filter, width=30); ent.pack(side="left", fill="x", expand=True)
        ent.bind("<KeyRelease>", lambda e: self._render_external())
        self.ext_status = ttk.Label(actions, text="")
        self.ext_status.pack(side="right", padx=(8,0))

        pane = ttk.Panedwindow(self.ext_tab, orient="vertical"); pane.pack(fill="both", expand=True, padx=8, pady=(0,8))
        table_frame = ttk.Frame(pane); detail_frame = ttk.LabelFrame(pane, text="Selected file details")
        pane.add(table_frame, weight=4); pane.add(detail_frame, weight=1)

        cols=("active","title","type","size","source")
        self.ext_tree=ttk.Treeview(table_frame, columns=cols, show="headings", selectmode="extended")
        self.ext_tree.heading("active", text="In HCS")
        self.ext_tree.heading("title", text="Title")
        self.ext_tree.heading("type", text="File")
        self.ext_tree.heading("size", text="Size")
        self.ext_tree.heading("source", text="Source")
        self.ext_tree.column("active", width=65, anchor="center", stretch=False)
        self.ext_tree.column("title", width=410)
        self.ext_tree.column("type", width=90, stretch=False)
        self.ext_tree.column("size", width=90, anchor="e", stretch=False)
        self.ext_tree.column("source", width=180)
        sb=ttk.Scrollbar(table_frame, orient="vertical", command=self.ext_tree.yview)
        self.ext_tree.configure(yscrollcommand=sb.set)
        self.ext_tree.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")
        self.ext_tree.bind("<<TreeviewSelect>>", self.external_show_details)

        self.ext_details=tk.Text(detail_frame, height=8, wrap="word", state="disabled")
        self.ext_details.pack(fill="both", expand=True, padx=6, pady=6)
        self.ext_docs=[]
        self.after(600, self.refresh_external)

    def _format_bytes(self, n):
        n=float(n or 0)
        for unit in ("B","KB","MB","GB"):
            if n < 1000 or unit == "GB": return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
            n /= 1000

    def refresh_external(self):
        root=self.hkr_root.get().strip()
        if not root: return
        try:
            self.ext_docs=api("GET", "/external/files?limit=5000&root_path=" + urllib.parse.quote(root))
            self._render_external()
        except Exception as e:
            self.ext_status.config(text="Unable to load library")
            messagebox.showerror("External Library", str(e))

    def _render_external(self):
        if not hasattr(self, "ext_tree"): return
        selected=set(self.ext_tree.selection())
        self.ext_tree.delete(*self.ext_tree.get_children())
        needle=self.ext_filter.get().strip().lower() if hasattr(self,"ext_filter") else ""
        shown=active_count=0
        for d in self.ext_docs:
            hay=" ".join([str(d.get("original_title") or ""), str(d.get("filename") or ""),
                          str(d.get("summary") or ""), " ".join(d.get("tags") or [])]).lower()
            if needle and needle not in hay: continue
            oid=d["object_id"]
            active=bool(d.get("active"))
            if active: active_count += 1
            self.ext_tree.insert("", "end", iid=oid, values=("YES" if active else "", d.get("original_title") or d.get("filename"),
                (d.get("filename") or "").rsplit(".",1)[-1].upper(), self._format_bytes(d.get("bytes")), d.get("source_domain") or ""))
            shown += 1
            if oid in selected: self.ext_tree.selection_add(oid)
        total_active=sum(1 for d in self.ext_docs if d.get("active"))
        self.ext_status.config(text=f"{total_active} active / {len(self.ext_docs)} in HKR" + (f" — {shown} shown" if needle else ""))

    def _external_selected_ids(self):
        return list(self.ext_tree.selection())

    def external_add_selected(self):
        ids=self._external_selected_ids()
        if not ids:
            messagebox.showinfo("External Library", "Select one or more HKR files first."); return
        try:
            out=api("POST", "/external/add", {"root_path":self.hkr_root.get().strip(), "object_ids":ids}, timeout=1800)
            self.refresh_external()
            messagebox.showinfo("External Library", f"Added {out.get('files_added',0)} file(s) to active HCS knowledge ({out.get('chunks',0)} chunks).")
        except Exception as e: messagebox.showerror("External Library", str(e))

    def external_remove_selected(self):
        ids=self._external_selected_ids()
        if not ids:
            messagebox.showinfo("External Library", "Select one or more HKR files first."); return
        try:
            out=api("POST", "/external/remove", {"root_path":self.hkr_root.get().strip(), "object_ids":ids})
            self.refresh_external()
            messagebox.showinfo("External Library", f"Removed {out.get('files_removed',0)} file(s) from active HCS knowledge. HKR originals were kept.")
        except Exception as e: messagebox.showerror("External Library", str(e))

    def external_show_details(self, event=None):
        ids=self._external_selected_ids()
        if not ids: return
        lookup={d.get("object_id"):d for d in self.ext_docs}
        d=lookup.get(ids[0]) or {}
        parts=[d.get("original_title") or d.get("filename") or ""]
        parts.append(f"Status: {'ACTIVE IN HCS' if d.get('active') else 'HKR LIBRARY ONLY'}")
        if d.get("summary"): parts.append("\nSummary:\n"+d["summary"])
        if d.get("tags"): parts.append("\nTags: "+", ".join(d["tags"]))
        if d.get("chapter_titles"): parts.append("\nSections: "+"; ".join(d["chapter_titles"][:12]))
        if d.get("index_pointer"): parts.append("\nIndex pointer: "+json.dumps(d["index_pointer"]))
        if d.get("path"): parts.append("\nFile: "+d["path"])
        self.ext_details.config(state="normal"); self.ext_details.delete("1.0","end")
        self.ext_details.insert("end", "\n".join(parts)); self.ext_details.config(state="disabled")

    def build_memory(self):
        top=ttk.Frame(self.mem_tab); top.pack(fill="x",padx=8,pady=8)
        ttk.Button(top,text="Add Memory",command=self.add_memory).pack(side="left")
        ttk.Button(top,text="Refresh",command=self.refresh_memory).pack(side="left",padx=8)
        self.mem_text=tk.Text(self.mem_tab,wrap="word"); self.mem_text.pack(fill="both",expand=True,padx=8,pady=(0,8))

    def add_memory(self):
        key=simpledialog.askstring("Memory","Key:")
        if not key:return
        value=simpledialog.askstring("Memory","Value:")
        if value is None:return
        try: api("POST","/memory",{"key":key,"value":value}); self.refresh_memory()
        except Exception as e: messagebox.showerror("Memory",str(e))

    def refresh_memory(self):
        try:
            out=api("GET","/memory"); self.mem_text.delete("1.0","end"); self.mem_text.insert("end",json.dumps(out,indent=2))
        except Exception as e: messagebox.showerror("Memory",str(e))

    def build_mcp(self):
        top=ttk.Frame(self.mcp_tab); top.pack(fill="x",padx=8,pady=8)
        ttk.Button(top,text="Register MCP Server",command=self.add_mcp).pack(side="left")
        ttk.Button(top,text="Refresh",command=self.refresh_mcp).pack(side="left",padx=8)
        ttk.Label(top,text="V1 registry; full MCP transport comes next.").pack(side="left",padx=12)
        self.mcp_text=tk.Text(self.mcp_tab,wrap="word"); self.mcp_text.pack(fill="both",expand=True,padx=8,pady=(0,8))

    def add_mcp(self):
        name=simpledialog.askstring("MCP","Server name:")
        if not name:return
        transport=simpledialog.askstring("MCP","Transport (stdio/http):",initialvalue="stdio") or "stdio"
        command=url=None; args=[]
        if transport=="stdio":
            command=simpledialog.askstring("MCP","Command/executable:")
            argline=simpledialog.askstring("MCP","Arguments (space-separated):",initialvalue="") or ""
            args=[x for x in argline.split(" ") if x]
        else: url=simpledialog.askstring("MCP","Server URL:")
        try:
            api("POST","/mcp/servers",{"name":name,"transport":transport,"command":command,"args":args,"url":url,"enabled":True})
            self.refresh_mcp()
        except Exception as e: messagebox.showerror("MCP",str(e))

    def refresh_mcp(self):
        try:
            out=api("GET","/mcp/servers"); self.mcp_text.delete("1.0","end"); self.mcp_text.insert("end",json.dumps(out,indent=2))
        except Exception as e: messagebox.showerror("MCP",str(e))

    def build_system(self):
        inference=ttk.LabelFrame(self.sys_tab,text="Internal AI Engine (llama.cpp)")
        inference.pack(fill="x",padx=8,pady=8)
        self.inference_status=ttk.Label(inference,text="Checking internal AI engine...")
        self.inference_status.grid(row=0,column=0,columnspan=5,sticky="w",padx=8,pady=6)
        ttk.Label(inference,text="GGUF model:").grid(row=1,column=0,sticky="w",padx=8,pady=6)
        self.model_path=tk.StringVar()
        ttk.Entry(inference,textvariable=self.model_path).grid(row=1,column=1,columnspan=3,sticky="ew",padx=4,pady=6)
        ttk.Button(inference,text="Browse",command=self.choose_model).grid(row=1,column=4,padx=8,pady=6)
        ttk.Button(inference,text="Save && Start",command=self.save_and_start_model).grid(row=2,column=0,padx=8,pady=6)
        ttk.Button(inference,text="Start",command=lambda:self.inference_action("start")).grid(row=2,column=1,padx=4,pady=6)
        ttk.Button(inference,text="Stop",command=lambda:self.inference_action("stop")).grid(row=2,column=2,padx=4,pady=6)
        ttk.Button(inference,text="Refresh",command=self.refresh_inference).grid(row=2,column=3,padx=4,pady=6)
        ttk.Button(inference,text="Internal AI Setup",command=self.launch_internal_setup).grid(row=2,column=4,padx=8,pady=6)
        inference.columnconfigure(1,weight=1)

        top=ttk.Frame(self.sys_tab); top.pack(fill="x",padx=8,pady=8)
        ttk.Button(top,text="System Info",command=lambda:self.call_sys("system_info",{})).pack(side="left")
        ttk.Button(top,text="Processes",command=lambda:self.call_sys("list_processes",{"limit":50})).pack(side="left",padx=8)
        ttk.Button(top,text="Env Names",command=lambda:self.call_sys("environment_variables",{})).pack(side="left")
        self.sys_text=tk.Text(self.sys_tab,wrap="word"); self.sys_text.pack(fill="both",expand=True,padx=8,pady=(0,8))
        self.after(700,self.refresh_inference)

    def choose_model(self):
        path=filedialog.askopenfilename(title="Choose a GGUF model",filetypes=[("GGUF models","*.gguf"),("All files","*.*")])
        if path:self.model_path.set(path)

    def refresh_inference(self):
        try:
            out=api("GET","/inference/status")
            if not self.model_path.get(): self.model_path.set(out.get("model_path", ""))
            state="Ready" if out.get("ready") else "Starting" if out.get("running") else "Stopped"
            missing=[]
            if not out.get("executable_found"): missing.append("engine not installed")
            if not out.get("model_found"): missing.append("model not found")
            detail=(" — " + ", ".join(missing)) if missing else ""
            port=f" on port {out['port']}" if out.get("port") else ""
            self.inference_status.config(text=f"{state}{port}{detail}")
        except Exception as e:
            self.inference_status.config(text=f"Status unavailable: {e}")

    def inference_action(self,action):
        try:
            api("POST",f"/inference/{action}",{})
            self.after(500,self.refresh_inference)
        except Exception as e: messagebox.showerror("Internal AI",str(e))

    def save_and_start_model(self):
        path=self.model_path.get().strip()
        if not path:return
        try:
            api("POST","/inference/config",{"model_path":path,"auto_start":True})
            self.after(500,self.refresh_inference)
        except Exception as e: messagebox.showerror("Internal AI",str(e))

    def launch_internal_setup(self):
        try:
            from .internal_ai_setup import existing_runtime_info, install_runtime
            existing = existing_runtime_info()
            if existing:
                sha = existing.get("llama_server_sha256", "unknown")
                messagebox.showinfo(
                    "Internal AI Setup",
                    "llama.cpp runtime is already installed.\n\n"
                    f"SHA-256: {sha}\n\n"
                    "If antivirus quarantined llama-server.exe, restore only that file "
                    "after verifying this hash/provenance, then click Refresh."
                )
                return

            import threading

            def work():
                try:
                    record = install_runtime()
                    self.after(0, lambda r=record: self._internal_ai_setup_done(r))
                except Exception as exc:
                    self.after(
                        0,
                        lambda m=str(exc): messagebox.showerror("Internal AI Setup", m),
                    )

            messagebox.showinfo(
                "Internal AI Setup",
                "HCS will download and install the official llama.cpp Windows CPU runtime. "
                "The installer now runs inside HCS instead of PowerShell."
            )
            threading.Thread(target=work, daemon=True).start()
        except Exception as e:
            messagebox.showerror("Internal AI Setup", str(e))

    def _internal_ai_setup_done(self, record):
        sha = record.get("llama_server_sha256", "unknown")
        tag = record.get("release_tag", "unknown")
        asset = record.get("asset_name", "unknown")
        messagebox.showinfo(
            "Internal AI Setup",
            "Internal AI runtime installed successfully.\n\n"
            f"Release: {tag}\n"
            f"Asset: {asset}\n"
            f"llama-server.exe SHA-256:\n{sha}\n\n"
            "If antivirus flags llama-server.exe, do not disable antivirus globally. "
            "Verify this provenance, restore only the executable, and use the narrowest "
            "runtime-folder exception needed. Then click Refresh and Start."
        )
        self.refresh_inference()

    def call_sys(self,name,args):
        try:
            out=api("POST","/tools/call",{"name":name,"args":args}); self.sys_text.delete("1.0","end"); self.sys_text.insert("end",json.dumps(out,indent=2))
        except Exception as e: messagebox.showerror("System",str(e))

if __name__ == "__main__":
    App().mainloop()
