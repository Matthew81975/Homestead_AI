"""Robot Command Center: persistent fleet, dock, and task management.

Simulation/planning only: this module never sends commands to physical robots.
Standard-library-only so it can be integrated with HCS desktop or API frontends.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCOPES = {"facility", "zone", "property"}
STATES = {"queued", "assigned", "working", "paused", "blocked", "completed", "failed", "cancelled"}
TRANSITIONS = {
    "queued": {"assigned", "cancelled"},
    "assigned": {"working", "queued", "cancelled"},
    "working": {"paused", "blocked", "completed", "failed", "cancelled"},
    "paused": {"working", "cancelled"},
    "blocked": {"queued", "cancelled"},
    "failed": {"queued", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _id():
    return str(uuid.uuid4())


class RobotStore:
    def __init__(self, path="data/robots/robots.sqlite3"):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS docks (
                id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                location TEXT NOT NULL, capacity INTEGER NOT NULL CHECK(capacity > 0),
                capabilities TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS robots (
                id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL, scope TEXT NOT NULL,
                area TEXT, preferred_dock_id TEXT REFERENCES docks(id) ON DELETE SET NULL,
                capabilities TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'idle',
                battery REAL CHECK(battery IS NULL OR (battery >= 0 AND battery <= 100)),
                simulated INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, instructions TEXT NOT NULL,
                category TEXT NOT NULL, priority INTEGER NOT NULL CHECK(priority BETWEEN 0 AND 3),
                location TEXT, destination TEXT, required_capabilities TEXT NOT NULL,
                robot_id TEXT REFERENCES robots(id) ON DELETE SET NULL,
                state TEXT NOT NULL DEFAULT 'queued',
                approval_required INTEGER NOT NULL DEFAULT 0,
                approved INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id),
                at TEXT NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL
            );
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def _get(self, table, item_id):
        row = self.db.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown {table} ID: {item_id}")
        return dict(row)

    def list(self, table):
        if table not in {"robots", "docks", "tasks"}:
            raise ValueError("Invalid resource")
        return [dict(row) for row in self.db.execute(f"SELECT * FROM {table} ORDER BY name" if table != "tasks" else "SELECT * FROM tasks ORDER BY priority DESC, created_at")]

    def add_dock(self, name, location, capacity=1, capabilities=()):
        if not name.strip() or not location.strip() or capacity < 1:
            raise ValueError("Dock name, location and positive capacity required")
        item_id = _id()
        with self.db:
            self.db.execute("INSERT INTO docks VALUES (?,?,?,?,?,1)",
                            (item_id, name.strip(), location.strip(), capacity, json.dumps(sorted(set(capabilities)))))
        return item_id

    def add_robot(self, name, role="general", scope="property", area=None,
                  preferred_dock_id=None, capabilities=(), simulated=True):
        if not name.strip() or scope not in SCOPES or (scope != "property" and not (area or "").strip()):
            raise ValueError("Robot name and valid operating area required")
        if preferred_dock_id:
            self._get("docks", preferred_dock_id)
        item_id = _id()
        with self.db:
            self.db.execute("""INSERT INTO robots
                (id,name,role,scope,area,preferred_dock_id,capabilities,simulated)
                VALUES (?,?,?,?,?,?,?,?)""",
                (item_id, name.strip(), role, scope, area, preferred_dock_id,
                 json.dumps(sorted(set(capabilities))), int(simulated)))
        return item_id

    def set_preferred_dock(self, robot_id, dock_id=None):
        self._get("robots", robot_id)
        if dock_id:
            self._get("docks", dock_id)
        with self.db:
            self.db.execute("UPDATE robots SET preferred_dock_id=? WHERE id=?", (dock_id, robot_id))

    def create_task(self, title, instructions="", category="general", priority=1,
                    location=None, destination=None, required_capabilities=(),
                    robot_id=None, approval_required=False):
        if not title.strip() or priority not in range(4):
            raise ValueError("Task title and priority (0-3) required")
        if robot_id:
            self._get("robots", robot_id)
        item_id, now = _id(), _now()
        with self.db:
            self.db.execute("""INSERT INTO tasks
                (id,title,instructions,category,priority,location,destination,
                 required_capabilities,robot_id,state,approval_required,approved,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item_id, title.strip(), instructions, category, priority, location, destination,
                 json.dumps(sorted(set(required_capabilities))), robot_id,
                 "assigned" if robot_id else "queued", int(approval_required), 0, now, now))
            self._event(item_id, "created", "Task registered")
        return item_id

    def _event(self, task_id, event, detail):
        self.db.execute("INSERT INTO task_events(task_id,at,event,detail) VALUES (?,?,?,?)",
                        (task_id, _now(), event, detail))

    def approve(self, task_id):
        self._get("tasks", task_id)
        with self.db:
            self.db.execute("UPDATE tasks SET approved=1,updated_at=? WHERE id=?", (_now(), task_id))
            self._event(task_id, "approved", "Human approval recorded")

    def assign(self, task_id, robot_id):
        task, robot = self._get("tasks", task_id), self._get("robots", robot_id)
        if task["state"] not in {"queued", "assigned"}:
            raise ValueError("Task cannot be reassigned in current state")
        if robot["status"] not in {"idle", "available"}:
            raise ValueError("Robot unavailable")
        required = set(json.loads(task["required_capabilities"]))
        if not required.issubset(set(json.loads(robot["capabilities"]))):
            raise ValueError("Robot lacks required capabilities")
        if robot["scope"] != "property" and task["location"] != robot["area"]:
            raise ValueError("Task outside robot operating area")
        with self.db:
            self.db.execute("UPDATE tasks SET robot_id=?,state='assigned',updated_at=? WHERE id=?",
                            (robot_id, _now(), task_id))
            self._event(task_id, "assigned", robot_id)

    def auto_assign(self, task_id):
        task = self._get("tasks", task_id)
        candidates = []
        for robot in self.list("robots"):
            try:
                if robot["simulated"] != 1:
                    continue  # No automatic physical robot dispatch in this release.
                if robot["status"] not in {"idle", "available"}:
                    continue
                if not set(json.loads(task["required_capabilities"])).issubset(set(json.loads(robot["capabilities"]))):
                    continue
                if robot["scope"] != "property" and robot["area"] != task["location"]:
                    continue
                candidates.append(robot)
            except (TypeError, ValueError):
                continue
        if not candidates:
            return None
        # Prefer local facility robots, then zone robots, then property-wide robots.
        candidates.sort(key=lambda r: ({"facility": 0, "zone": 1, "property": 2}[r["scope"]], r["name"]))
        self.assign(task_id, candidates[0]["id"])
        return candidates[0]["id"]

    def transition(self, task_id, state, detail=""):
        task = self._get("tasks", task_id)
        if state not in STATES or state not in TRANSITIONS[task["state"]]:
            raise ValueError(f"Invalid transition: {task['state']} -> {state}")
        if state == "working":
            if not task["robot_id"]:
                raise ValueError("Assign a robot before starting")
            if task["approval_required"] and not task["approved"]:
                raise ValueError("Human approval required")
            if not self._get("robots", task["robot_id"])["simulated"]:
                raise ValueError("Physical robot execution is not enabled")
        with self.db:
            self.db.execute("UPDATE tasks SET state=?,updated_at=? WHERE id=?", (state, _now(), task_id))
            self._event(task_id, state, detail)

    def events(self, task_id):
        self._get("tasks", task_id)
        return [dict(r) for r in self.db.execute(
            "SELECT at,event,detail FROM task_events WHERE task_id=? ORDER BY id", (task_id,))]
