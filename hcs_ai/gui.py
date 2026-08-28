import json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
import urllib.request, urllib.parse
import os
import subprocess
from pathlib import Path
from .ports import port_candidates, saved_endpoint

BASE = None


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
        ttk.Button(header, text="Expand", command=self._expand_ai_console).pack(side="right")
        ttk.Button(header, text="Normal", command=self._set_ai_console_normal).pack(side="right", padx=4)
        ttk.Button(header, text="Collapse", command=self._collapse_ai_console).pack(side="right")

        chat_area = ttk.Frame(self.chat_tab)
        chat_area.pack(fill="both", expand=True, padx=8, pady=6)
        self.chat_box = tk.Text(chat_area, wrap="word", state="disabled", height=8)
        chat_scroll = ttk.Scrollbar(chat_area, orient="vertical", command=self.chat_box.yview)
        self.chat_box.configure(yscrollcommand=chat_scroll.set)
        self.chat_box.pack(side="left", fill="both", expand=True)
        chat_scroll.pack(side="right", fill="y")

        row = ttk.Frame(self.chat_tab); row.pack(fill="x", padx=8, pady=(0,6))
        self.prompt = ttk.Entry(row); self.prompt.pack(side="left", fill="x", expand=True)
        self.prompt.bind("<Return>", lambda e: self.send())
        ttk.Button(row, text="Send", command=self.send).pack(side="left", padx=(8,0))
        self.use_kb = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="Use KB", variable=self.use_kb).pack(side="left", padx=8)
        self.status = ttk.Label(self.chat_tab, text="Checking server...")
        self.status.pack(anchor="w", padx=8, pady=(0,6))

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

    def append_chat(self, who, text):
        self.chat_box.config(state="normal")
        self.chat_box.insert("end", f"{who}:\n{text}\n\n")
        self.chat_box.see("end"); self.chat_box.config(state="disabled")

    def send(self):
        msg = self.prompt.get().strip()
        if not msg: return
        self.prompt.delete(0, "end"); self.append_chat("You", msg); self.update()
        try:
            out = api("POST", "/chat", {"message":msg,"history":self.history[-12:],"use_kb":self.use_kb.get()})
            text = out.get("text",""); self.append_chat("HCS-AI", text)
            self.history += [{"role":"user","content":msg},{"role":"assistant","content":text}]
            if out.get("tool_results"):
                self.append_chat("Tools", json.dumps(out["tool_results"], indent=2)[:5000])
        except Exception as e:
            self.append_chat("Error", str(e))

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
        script=Path(__file__).resolve().parent.parent / "setup_internal_ai.ps1"
        try:
            subprocess.Popen(["powershell.exe","-NoProfile","-ExecutionPolicy","Bypass","-File",str(script)])
            messagebox.showinfo("Internal AI Setup","The setup window is open. Refresh or restart HCS-AI when it finishes.")
        except Exception as e: messagebox.showerror("Internal AI Setup",str(e))

    def call_sys(self,name,args):
        try:
            out=api("POST","/tools/call",{"name":name,"args":args}); self.sys_text.delete("1.0","end"); self.sys_text.insert("end",json.dumps(out,indent=2))
        except Exception as e: messagebox.showerror("System",str(e))

if __name__ == "__main__":
    App().mainloop()
