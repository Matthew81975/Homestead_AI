"""Noninteractive Local Codex construction and execution for HCS."""

from __future__ import annotations

from pathlib import Path

from .actions import ActionExecutor, CommandPolicy, detect_interactive_launch_intent
from .controller import AgentController
from .lm_client import LMStudioClient
from .project_control import ProjectControlClient
from .state import AgentStatus, TaskJournal
from .tornado import TornadoClient
from .workspace import Workspace
from .workspace_registry import WorkspaceRegistry


def make_client(config: dict, *, status_callback=None):
    tornado = config.get("tornado") or {}
    if tornado.get("enabled"):
        return TornadoClient.from_config(config, status_callback=status_callback)
    return LMStudioClient(
        config["lm_studio_url"],
        config["model"],
        timeout=float(config.get("lm_timeout_seconds", 600)),
    )


def _workspace_config(registry_path: Path, workspace_path: Path):
    if not registry_path.exists():
        return None
    registry = WorkspaceRegistry.load(registry_path)
    target = workspace_path.resolve()
    for workspace_config in registry.all_enabled():
        try:
            configured = Path(workspace_config.path).resolve()
        except OSError:
            continue
        if configured == target:
            return workspace_config
    return None


def build_controller(
    config: dict,
    journal: TaskJournal,
    *,
    dry_run: bool = False,
    workspaces_config_path: Path,
    status_callback=None,
    approval_callback=None,
    cancel_event=None,
) -> AgentController:
    """Build the restricted runtime without starting a task or prompting.

    The controller stops at READY_FOR_APPROVAL; it never grants commit or push
    approval. ``run_task`` may then request final task review from HCS.
    Cancellation is cooperative between controller action-loop iterations.
    """
    status = status_callback or (lambda _message: None)
    workspace_path = Path(config["workspace"])
    workspace = Workspace(workspace_path)
    client = make_client(config, status_callback=status)
    workspace_config = _workspace_config(Path(workspaces_config_path), workspace_path)
    project_control = (
        ProjectControlClient(
            workspace_config.control_api_url,
            discovery_path=workspace_path / ".maze_world_control.json",
        )
        if workspace_config is not None and workspace_config.control_api_url
        else None
    )
    executor = ActionExecutor(
        workspace=workspace,
        journal=journal,
        dry_run=dry_run,
        command_policy=CommandPolicy({"run_maze.bat"}),
        allow_interactive_launch=detect_interactive_launch_intent(journal.task),
        project_control=project_control,
        tornado_client=(client if callable(getattr(client, "status_snapshot", None)) else None),
    )
    return AgentController(
        client,
        executor,
        journal,
        config["max_failed_actions"],
        status_callback=status,
        cancel_event=cancel_event,
    )


def run_task(
    config: dict,
    journal: TaskJournal,
    *,
    dry_run: bool = False,
    workspaces_config_path: Path,
    status_callback=None,
    approval_callback=None,
    cancel_event=None,
) -> AgentStatus:
    """Run a task silently by default and return its final controller status."""
    status = status_callback or (lambda _message: None)
    controller = build_controller(
        config=config,
        journal=journal,
        dry_run=dry_run,
        workspaces_config_path=workspaces_config_path,
        status_callback=status,
        approval_callback=approval_callback,
        cancel_event=cancel_event,
    )
    result = controller.run().status
    if result == AgentStatus.READY_FOR_APPROVAL and approval_callback is not None:
        approved = approval_callback({"kind": "task_review", "summary": journal.task})
        if cancel_event is not None and cancel_event.is_set():
            result = AgentStatus.INTERRUPTED
        else:
            result = AgentStatus.DONE if approved else AgentStatus.BLOCKED
        # Review does not authorize or execute a commit or push.
        journal.status = result
        journal.save()
    return result
