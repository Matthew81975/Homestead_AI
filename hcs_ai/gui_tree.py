import json
import os
import urllib.parse
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .gui import App as BaseApp, api


class App(BaseApp):
    def build_kb(self):
        top = ttk.Frame(self.kb_tab)
        top.pack(fill="x", padx=8, pady=8)
        ttk.Button(top, text="Import File", command=self.import_file).pack(side="left")
        ttk.Button(top, text="Import Folder", command=self.import_folder).pack(side="left", padx=8)
        ttk.Button(top, text="Refresh Tree", command=self.refresh_knowledge_tree).pack(side="left")
        self.kb_tree_status = ttk.Label(top, text="Knowledge Tree not loaded")
        self.kb_tree_status.pack(side="left", padx=12)

        work = ttk.Notebook(self.kb_tab)
        work.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        tree_tab = ttk.Frame(work)
        search_tab = ttk.Frame(work)
        work.add(tree_tab, text="Knowledge Tree")
        work.add(search_tab, text="Search")

        pane = ttk.Panedwindow(tree_tab, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=4, pady=4)
        left = ttk.Frame(pane)
        right = ttk.Frame(pane)
        pane.add(left, weight=3)
        pane.add(right, weight=2)

        self.knowledge_tree = ttk.Treeview(
            left,
            columns=("kind", "status", "confidence"),
            show="tree headings",
            selectmode="browse",
        )
        self.knowledge_tree.heading("#0", text="Field / Artifact")
        self.knowledge_tree.heading("kind", text="Type")
        self.knowledge_tree.heading("status", text="Status")
        self.knowledge_tree.heading("confidence", text="Confidence")
        self.knowledge_tree.column("#0", width=360, minwidth=220)
        self.knowledge_tree.column("kind", width=110, minwidth=85, stretch=False)
        self.knowledge_tree.column("status", width=100, minwidth=80, stretch=False)
        self.knowledge_tree.column("confidence", width=90, minwidth=75, stretch=False, anchor="center")
        tree_scroll = ttk.Scrollbar(left, orient="vertical", command=self.knowledge_tree.yview)
        self.knowledge_tree.configure(yscrollcommand=tree_scroll.set)
        self.knowledge_tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")
        self.knowledge_tree.bind("<<TreeviewSelect>>", self.show_knowledge_detail)
        self.knowledge_tree.bind("<Double-1>", self.open_selected_knowledge_artifact)

        detail_toolbar = ttk.Frame(right)
        detail_toolbar.pack(fill="x", pady=(0, 4))
        ttk.Label(detail_toolbar, text="Details").pack(side="left")
        ttk.Button(
            detail_toolbar, text="Open Artifact", command=self.open_selected_knowledge_artifact
        ).pack(side="right")

        self.kb_detail = tk.Text(right, wrap="word", state="disabled")
        detail_scroll = ttk.Scrollbar(right, orient="vertical", command=self.kb_detail.yview)
        self.kb_detail.configure(yscrollcommand=detail_scroll.set)
        self.kb_detail.pack(side="left", fill="both", expand=True)
        detail_scroll.pack(side="right", fill="y")

        search_top = ttk.Frame(search_tab)
        search_top.pack(fill="x", padx=6, pady=6)
        ttk.Label(search_top, text="Search active knowledge:").pack(side="left")
        self.kb_query = ttk.Entry(search_top)
        self.kb_query.pack(side="left", fill="x", expand=True, padx=8)
        self.kb_query.bind("<Return>", lambda _e: self.search_kb())
        ttk.Button(search_top, text="Search", command=self.search_kb).pack(side="left")
        self.kb_results = tk.Text(search_tab, wrap="word")
        self.kb_results.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        self.kb_tree_data = {"nodes": [], "links": [], "relationships": [], "counts": {}}
        self.kb_tree_items = {}
        self.after(1500, self.refresh_knowledge_tree)

    def _import(self, path):
        if not path:
            return
        try:
            out = api("POST", "/knowledge/import", {"path": path})
            classified = out.get("artifacts_classified", 0)
            errors = out.get("classification_errors") or []
            msg = (
                f"Imported {out.get('files_imported', 0)} files / {out.get('chunks', 0)} chunks.\n"
                f"LLM classified {classified} artifact(s) into the Knowledge Tree."
            )
            if errors:
                msg += f"\n{len(errors)} artifact(s) could not be classified."
            messagebox.showinfo("Knowledge Base", msg)
            self.refresh_knowledge_tree()
        except Exception as e:
            messagebox.showerror("Knowledge Base", str(e))

    def import_file(self):
        self._import(filedialog.askopenfilename())

    def import_folder(self):
        self._import(filedialog.askdirectory())

    def search_kb(self):
        q = self.kb_query.get().strip()
        if not q:
            return
        try:
            out = api("GET", "/knowledge/search?q=" + urllib.parse.quote(q))
            self.kb_results.delete("1.0", "end")
            self.kb_results.insert("end", json.dumps(out, indent=2))
        except Exception as e:
            messagebox.showerror("Search", str(e))

    def refresh_knowledge_tree(self):
        try:
            data = api("GET", "/knowledge/tree")
        except Exception as e:
            self.kb_tree_status.config(text=f"Tree unavailable: {e}")
            return

        self.kb_tree_data = data
        tree = self.knowledge_tree
        tree.delete(*tree.get_children())
        self.kb_tree_items = {}

        nodes = {int(n["id"]): n for n in data.get("nodes", [])}
        children = {}
        for node in nodes.values():
            parent = node.get("parent_id")
            parent = int(parent) if parent is not None else None
            children.setdefault(parent, []).append(node)

        links_by_node = {}
        for link in data.get("links", []):
            links_by_node.setdefault(int(link["node_id"]), []).append(link)

        def add_node(node, parent_item=""):
            node_id = int(node["id"])
            pending = node.get("review_status") == "pending_review"
            label = ("[review] " if pending else "") + str(node.get("canonical_name") or "Unnamed")
            confidence = node.get("confidence")
            confidence_text = f"{float(confidence):.2f}" if confidence is not None else ""
            item_id = f"n:{node_id}"
            tree.insert(
                parent_item,
                "end",
                iid=item_id,
                text=label,
                values=("field", node.get("review_status", ""), confidence_text),
                open=(parent_item == ""),
            )
            self.kb_tree_items[item_id] = ("node", node)

            for child in sorted(
                children.get(node_id, []),
                key=lambda x: str(x.get("canonical_name") or "").lower(),
            ):
                add_node(child, item_id)

            for link in sorted(
                links_by_node.get(node_id, []),
                key=lambda x: str(x.get("title") or "").lower(),
            ):
                artifact_id = int(link["artifact_id"])
                leaf_id = f"a:{node_id}:{artifact_id}"
                title = str(link.get("title") or f"Artifact {artifact_id}")
                if link.get("is_primary"):
                    title = "★ " + title
                link_conf = link.get("confidence")
                link_conf_text = f"{float(link_conf):.2f}" if link_conf is not None else ""
                tree.insert(
                    item_id,
                    "end",
                    iid=leaf_id,
                    text=title,
                    values=(link.get("artifact_type") or "artifact", "linked", link_conf_text),
                )
                self.kb_tree_items[leaf_id] = ("artifact", link)

        for root in sorted(
            children.get(None, []),
            key=lambda x: str(x.get("canonical_name") or "").lower(),
        ):
            add_node(root)

        counts = data.get("counts") or {}
        self.kb_tree_status.config(
            text=(
                f"{counts.get('nodes', 0)} fields · "
                f"{counts.get('artifacts', 0)} artifacts · "
                f"{counts.get('links', 0)} links"
            )
        )
        if not nodes:
            self._set_kb_detail(
                "The Knowledge Tree is empty.\n\nImport material and the local LLM will analyze it, "
                "create/reuse subject branches, and link each artifact into the appropriate places."
            )

    def _set_kb_detail(self, text):
        self.kb_detail.config(state="normal")
        self.kb_detail.delete("1.0", "end")
        self.kb_detail.insert("end", text)
        self.kb_detail.config(state="disabled")

    def show_knowledge_detail(self, event=None):
        selected = self.knowledge_tree.selection()
        if not selected:
            return
        item_id = selected[0]
        record = self.kb_tree_items.get(item_id)
        if not record:
            return
        kind, data = record

        if kind == "node":
            node_id = int(data["id"])
            parts = [str(data.get("canonical_name") or "Unnamed field")]
            parts.append(f"Status: {data.get('review_status', 'accepted')}")
            if data.get("confidence") is not None:
                parts.append(f"Taxonomy confidence: {float(data['confidence']):.2f}")
            if data.get("description"):
                parts.append("\nDescription:\n" + str(data["description"]))
            if data.get("aliases"):
                parts.append("\nAliases: " + ", ".join(data["aliases"]))
            if data.get("created_by_model"):
                parts.append("\nCreated by model: " + str(data["created_by_model"]))

            direct = [x for x in self.kb_tree_data.get("links", []) if int(x["node_id"]) == node_id]
            parts.append(f"\nDirectly linked artifacts: {len(direct)}")
            if direct:
                parts.extend("  • " + str(x.get("title") or x.get("artifact_id")) for x in direct[:30])

            relations = []
            for rel in self.kb_tree_data.get("relationships", []):
                if int(rel["source_node_id"]) == node_id:
                    relations.append(
                        f"{rel['relationship_type']} → {rel['target_name']} ({float(rel['confidence']):.2f})"
                    )
                elif int(rel["target_node_id"]) == node_id:
                    relations.append(
                        f"{rel['source_name']} → {rel['relationship_type']} ({float(rel['confidence']):.2f})"
                    )
            if relations:
                parts.append("\nRelationships:\n" + "\n".join("  • " + r for r in relations[:30]))
            self._set_kb_detail("\n".join(parts))
            return

        parts = [str(data.get("title") or f"Artifact {data.get('artifact_id')}")]
        parts.append(f"Type: {data.get('artifact_type', 'artifact')}")
        parts.append(f"Relationship: {data.get('relationship_type', 'about')}")
        if data.get("confidence") is not None:
            parts.append(f"Link confidence: {float(data['confidence']):.2f}")
        parts.append("Primary classification: " + ("yes" if data.get("is_primary") else "no"))
        if data.get("summary"):
            parts.append("\nSummary:\n" + str(data["summary"]))
        if data.get("storage_uri"):
            parts.append("\nLocation:\n" + str(data["storage_uri"]))
        if data.get("content_hash"):
            parts.append("\nSHA-256: " + str(data["content_hash"]))
        metadata = data.get("metadata") or {}
        if metadata:
            parts.append("\nMetadata:\n" + json.dumps(metadata, indent=2))
        self._set_kb_detail("\n".join(parts))

    def open_selected_knowledge_artifact(self, event=None):
        selected = self.knowledge_tree.selection()
        if not selected:
            return
        record = self.kb_tree_items.get(selected[0])
        if not record or record[0] != "artifact":
            return
        uri = record[1].get("storage_uri")
        if not uri:
            messagebox.showinfo("Knowledge Tree", "This artifact does not have a local file location.")
            return
        if str(uri).startswith(("http://", "https://")):
            webbrowser.open(str(uri))
            return
        path = Path(uri)
        if not path.exists():
            messagebox.showerror("Knowledge Tree", f"Artifact file was not found:\n{uri}")
            return
        try:
            if os.name == "nt":
                os.startfile(str(path))
            else:
                import subprocess
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as e:
            messagebox.showerror("Knowledge Tree", str(e))


if __name__ == "__main__":
    App().mainloop()
