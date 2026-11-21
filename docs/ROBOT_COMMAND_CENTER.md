# Robot Command Center (initial implementation)

This branch introduces a standalone, standard-library-only Robot Command Center
prototype for HCS. It does **not** send instructions to real robots.

## Modules

- `hcs_robot_command.py`: SQLite persistence for robots, docks, tasks and task events.
- `hcs_robot_tab.py`: embeddable Tkinter Notebook tab with robot registration,
  preferred docking station, operating scope, dock creation, and task assignment.
- `tests/test_robot_command.py`: basic persistence, dispatch and safety tests.

## Try it

From the repository root with Python 3.10+:

```powershell
python -m unittest discover -s tests -p test_robot_command.py
python hcs_robot_tab.py
```

## Integrating with the existing HCS GUI

Import `RobotCommandTab` and add it to the application's existing notebook:

```python
from hcs_robot_tab import RobotCommandTab
notebook.add(RobotCommandTab(notebook), text="Robots")
```

The exact integration point must be validated against the existing GUI framework.
If HCS uses a web frontend, expose the store through authenticated endpoints instead
of embedding Tkinter. Do not create unauthenticated robot-control endpoints.

## Design constraints and next steps

- Preferred dock is optional and not a hard operational boundary.
- Facility and zone assignments restrict task eligibility; property robots roam.
- Automatic selection favors a suitable local robot over a property-wide robot.
- Dock records contain capacity and supported services; reservations, power
  budgeting and physical docking protocol remain future work.
- Tasks have lifecycle transitions, human approval gating, and an audit trail.
- Physical execution is explicitly blocked. No physical safety assurance is claimed.
- Next: inspect HCS application startup, mount the tab in its real GUI,
  add robot/dock edit and deletion, task controls, telemetry, and scheduler.
- Next: add route/energy-aware docking selection, dock reservations, recurring
  tasks, task dependencies, and a simulation adapter.

Expected software dependencies: $0. Hardware integration is not included.
