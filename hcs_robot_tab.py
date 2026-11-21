"""Embeddable Tkinter Robot Command Center tab for HCS desktop applications."""
import json
import tkinter as tk
from tkinter import messagebox, ttk

from hcs_robot_command import RobotStore


class RobotCommandTab(ttk.Frame):
    def __init__(self, parent, store=None):
        super().__init__(parent)
        self.store = store or RobotStore()
        self._owns_store = store is None
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=8, pady=8)
        self._build_robots()
        self._build_docks()
        self._build_tasks()
        self.refresh()

    def _page(self, title):
        page = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(page, text=title)
        return page

    def _field(self, parent, label, default=""):
        frame = ttk.Frame(parent)
        frame.pack(fill="x", pady=3)
        ttk.Label(frame, text=label, width=22).pack(side="left")
        var = tk.StringVar(value=default)
        ttk.Entry(frame, textvariable=var).pack(side="left", fill="x", expand=True)
        return var

    def _combo(self, parent, label, values, default=None):
        frame = ttk.Frame(parent)
        frame.pack(fill="x", pady=3)
        ttk.Label(frame, text=label, width=22).pack(side="left")
        var = tk.StringVar(value=default or values[0])
        ttk.Combobox(frame, textvariable=var, values=values, state="readonly").pack(
            side="left", fill="x", expand=True)
        return var

    def _table(self, page, columns):
        table = ttk.Treeview(page, columns=columns, show="headings", height=9)
        for column in columns:
            table.heading(column, text=column.replace("_", " ").title())
            table.column(column, width=140, stretch=True)
        table.pack(fill="both", expand=True, pady=8)
        return table

    def _build_robots(self):
        page = self._page("Robots")
        self.robot_table = self._table(page, ("name", "role", "scope", "area", "preferred_dock", "status"))
        form = ttk.LabelFrame(page, text="Add Robot", padding=8)
        form.pack(fill="x")
        self.r_name = self._field(form, "Robot name")
        self.r_role = self._combo(form, "Role", ["general", "transport", "inspection", "maintenance", "agriculture"])
        self.r_scope = self._combo(form, "Operating scope", ["property", "facility", "zone"])
        self.r_area = self._field(form, "Facility / zone")
        self.r_dock = self._combo(form, "Preferred docking station", ["(none)"])
        self.r_cap = self._field(form, "Capabilities (comma-separated)")
        ttk.Button(form, text="Add Robot", command=self._add_robot).pack(anchor="e", pady=4)

    def _build_docks(self):
        page = self._page("Docking Stations")
        self.dock_table = self._table(page, ("name", "location", "capacity", "capabilities"))
        form = ttk.LabelFrame(page, text="Add Docking Station", padding=8)
        form.pack(fill="x")
        self.d_name = self._field(form, "Dock name")
        self.d_location = self._field(form, "Location / facility")
        self.d_capacity = self._field(form, "Number of ports", "1")
        self.d_cap = self._field(form, "Services (comma-separated)", "charging,data,diagnostics")
        ttk.Button(form, text="Add Docking Station", command=self._add_dock).pack(anchor="e", pady=4)

    def _build_tasks(self):
        page = self._page("Tasks")
        self.task_table = self._table(page, ("title", "priority", "state", "robot", "location"))
        form = ttk.LabelFrame(page, text="Create & Assign Task", padding=8)
        form.pack(fill="x")
        self.t_title = self._field(form, "Task name")
        self.t_instructions = self._field(form, "Instructions")
        self.t_location = self._field(form, "Work location")
        self.t_destination = self._field(form, "Destination (optional)")
        self.t_category = self._combo(form, "Category", ["general", "transport", "inspection", "maintenance", "agriculture"])
        self.t_priority = self._combo(form, "Priority", ["normal", "low", "high", "urgent"])
        self.t_cap = self._field(form, "Required capabilities")
        self.t_robot = self._combo(form, "Assignment", ["Automatic"])
        self.t_approval = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="Require approval before execution",
                        variable=self.t_approval).pack(anchor="w")
        ttk.Button(form, text="Create Task", command=self._add_task).pack(anchor="e", pady=4)

    @staticmethod
    def _split(value):
        return [v.strip() for v in value.split(",") if v.strip()]

    def _run(self, action):
        try:
            action()
            self.refresh()
        except (ValueError, TypeError, KeyError, __import__("sqlite3").IntegrityError) as exc:
            messagebox.showerror("Robot Command Center", str(exc), parent=self)

    def _add_robot(self):
        def action():
            dock_id = self._dock_names.get(self.r_dock.get())
            self.store.add_robot(self.r_name.get(), self.r_role.get(), self.r_scope.get(),
                                 self.r_area.get() or None, dock_id, self._split(self.r_cap.get()))
            self.r_name.set("")
        self._run(action)

    def _add_dock(self):
        def action():
            self.store.add_dock(self.d_name.get(), self.d_location.get(),
                                int(self.d_capacity.get()), self._split(self.d_cap.get()))
            self.d_name.set("")
        self._run(action)

    def _add_task(self):
        def action():
            priority = {"low": 0, "normal": 1, "high": 2, "urgent": 3}[self.t_priority.get()]
            robot_id = self._robot_names.get(self.t_robot.get())
            task_id = self.store.create_task(
                self.t_title.get(), self.t_instructions.get(), self.t_category.get(),
                priority, self.t_location.get() or None, self.t_destination.get() or None,
                self._split(self.t_cap.get()), robot_id, self.t_approval.get())
            if self.t_robot.get() == "Automatic":
                self.store.auto_assign(task_id)
            self.t_title.set("")
        self._run(action)

    def refresh(self):
        robots, docks, tasks = (self.store.list(name) for name in ("robots", "docks", "tasks"))
        self._dock_names = {d["name"]: d["id"] for d in docks}
        self._robot_names = {r["name"]: r["id"] for r in robots}
        self.r_dock.set("(none)")
        self.r_dock_widget_values = ["(none)"] + list(self._dock_names)
        self._set_combo_values(self.r_dock, self.r_dock_widget_values)
        self._set_combo_values(self.t_robot, ["Automatic"] + list(self._robot_names))
        self._populate(self.dock_table, [(d["name"], d["location"], d["capacity"],
                      ", ".join(json.loads(d["capabilities"]))) for d in docks])
        dock_by_id = {d["id"]: d["name"] for d in docks}
        robot_by_id = {r["id"]: r["name"] for r in robots}
        self._populate(self.robot_table, [(r["name"], r["role"], r["scope"], r["area"] or "",
                      dock_by_id.get(r["preferred_dock_id"], ""), r["status"]) for r in robots])
        self._populate(self.task_table, [(t["title"], t["priority"], t["state"],
                      robot_by_id.get(t["robot_id"], ""), t["location"] or "") for t in tasks])

    def _set_combo_values(self, variable, values):
        for widget in self.winfo_children():
            self._find_combo(widget, variable, values)

    def _find_combo(self, parent, variable, values):
        for child in parent.winfo_children():
            if isinstance(child, ttk.Combobox) and str(child.cget("textvariable")) == str(variable):
                child.configure(values=values)
                if variable.get() not in values:
                    variable.set(values[0])
            self._find_combo(child, variable, values)

    @staticmethod
    def _populate(table, rows):
        table.delete(*table.get_children())
        for row in rows:
            table.insert("", "end", values=row)


if __name__ == "__main__":
    root = tk.Tk()
    root.title("HCS Robot Command Center — Simulation")
    root.geometry("950x750")
    RobotCommandTab(root).pack(fill="both", expand=True)
    root.mainloop()
