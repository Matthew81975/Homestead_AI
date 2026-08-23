from __future__ import annotations

import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import ttk

from .gui_tree import App as TreeApp
from .homepage import homepage_path

HOME_TAB_TITLE = "Home"


def insert_home_tab(notebook, frame) -> None:
    """Insert Matt's Laboratory as the first top-level HCS tab."""
    notebook.insert(0, frame, text=HOME_TAB_TITLE)


class App(TreeApp):
    """Knowledge-tree GUI plus the integrated Matt's Laboratory homepage."""

    def __init__(self):
        super().__init__()
        notebook = next(
            (child for child in self.winfo_children() if isinstance(child, ttk.Notebook)),
            None,
        )
        if notebook is None:
            raise RuntimeError("HCS top-level notebook was not found.")

        self.home_tab = ttk.Frame(notebook)
        insert_home_tab(notebook, self.home_tab)
        self._home_renderer = None
        self._build_home()

    def _build_home(self) -> None:
        toolbar = ttk.Frame(self.home_tab)
        toolbar.pack(fill="x", padx=8, pady=8)
        ttk.Label(toolbar, text="Matt's Laboratory").pack(side="left")
        ttk.Button(toolbar, text="Reload", command=self._reload_home).pack(side="right")
        ttk.Button(toolbar, text="Open in Browser", command=self._open_home_browser).pack(
            side="right", padx=(0, 8)
        )

        self.home_content = ttk.Frame(self.home_tab)
        self.home_content.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self._reload_home()

    def _clear_home_content(self) -> None:
        for child in self.home_content.winfo_children():
            child.destroy()
        self._home_renderer = None

    def _reload_home(self) -> None:
        self._clear_home_content()
        path = homepage_path()
        if not path.exists():
            ttk.Label(
                self.home_content,
                text=f"Homepage file was not found:\n{path}",
                justify="left",
            ).pack(anchor="nw", padx=12, pady=12)
            return

        try:
            from tkinterweb import HtmlFrame

            frame = HtmlFrame(self.home_content, messages_enabled=False)
            frame.pack(fill="both", expand=True)
            frame.load_file(str(path))
            self._home_renderer = frame
        except Exception as exc:
            fallback = ttk.Frame(self.home_content)
            fallback.pack(fill="both", expand=True)
            ttk.Label(
                fallback,
                text=(
                    "The embedded homepage renderer could not start.\n"
                    "Matt's Laboratory is still available in your browser.\n\n"
                    f"{exc}"
                ),
                justify="left",
                wraplength=760,
            ).pack(anchor="nw", padx=12, pady=12)
            ttk.Button(fallback, text="Open Matt's Laboratory", command=self._open_home_browser).pack(
                anchor="nw", padx=12
            )

    def _open_home_browser(self) -> None:
        path: Path = homepage_path()
        webbrowser.open(path.as_uri())
