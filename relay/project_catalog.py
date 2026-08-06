"""Trusted Herdr Plus project discovery and activation."""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 runtime fallback
    import tomli as tomllib


PROJECT_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")
AGENT_DEFINITIONS = (
    {
        "id": "codex", "label": "Codex", "executable": "codex",
        "paths": ("~/.local/bin/codex",),
    },
    {
        "id": "claude", "label": "Claude", "executable": "claude",
        "paths": ("~/.local/bin/claude", "~/.claude/local/claude"),
    },
    {
        "id": "opencode", "label": "OpenCode", "executable": "opencode",
        "paths": ("~/.opencode/bin/opencode", "~/.local/bin/opencode"),
    },
    {
        "id": "cursor", "label": "Cursor", "executable": "cursor-agent",
        "paths": ("~/.local/bin/cursor-agent",),
    },
)
MAX_TABS = 24
MAX_PANES_PER_TAB = 4


class ProjectCatalogError(ValueError):
    pass


def discover_projects_dir(runner, configured: str = "") -> Path:
    if configured:
        return Path(configured).expanduser().resolve()
    ok, output = runner("plugin", "config-dir", "cloudmanic.herdr-plus")
    if not ok or not output.strip():
        raise ProjectCatalogError("Herdr Plus project configuration is unavailable")
    return (Path(output.strip()).expanduser() / "projects").resolve()


def load_projects(projects_dir: Path) -> list[dict]:
    if not projects_dir.is_dir():
        raise ProjectCatalogError("Herdr Plus projects directory is unavailable")
    projects = []
    seen_names = set()
    for path in sorted(projects_dir.glob("*.toml")):
        project_id = path.stem
        if not PROJECT_ID_RE.fullmatch(project_id) or path.is_symlink():
            continue
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ProjectCatalogError(f"Invalid project template: {path.name}") from exc
        project = _normalize_project(project_id, data, path.name)
        folded_name = project["name"].casefold()
        if folded_name in seen_names:
            raise ProjectCatalogError(f"Duplicate project name: {project['name']}")
        seen_names.add(folded_name)
        projects.append(project)
    return sorted(projects, key=lambda item: (item["group"].casefold(), item["name"].casefold()))


def available_agents(runner, path_lookup=shutil.which, executable_check=None) -> list[dict]:
    check_executable = executable_check or _path_is_executable
    ok, integration_status = runner("integration", "status")
    if not ok:
        raise ProjectCatalogError("Could not inspect Herdr agent integrations")
    current_integrations = {
        line.split(":", 1)[0].strip()
        for line in integration_status.splitlines()
        if ": current " in line
    }
    return [
        {"id": agent["id"], "label": agent["label"]}
        for agent in AGENT_DEFINITIONS
        if agent["id"] in current_integrations and (
            path_lookup(agent["executable"]) or any(
                check_executable(Path(path).expanduser()) for path in agent["paths"]
            )
        )
    ]


def _path_is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def public_projects(
    projects: list[dict], active_workspaces: list[dict], active_agents: list[dict] = (),
) -> list[dict]:
    active_by_name = {
        str(workspace.get("label", "")).casefold(): str(workspace.get("workspace_id", ""))
        for workspace in active_workspaces
        if workspace.get("label") and workspace.get("workspace_id")
    }
    agent_workspaces = {
        str(agent.get("workspace_id", ""))
        for agent in active_agents
        if agent.get("workspace_id")
    }
    agent_by_workspace = {
        str(agent.get("workspace_id", "")): str(agent.get("agent", ""))
        for agent in active_agents
        if agent.get("workspace_id") and agent.get("agent")
    }
    return [
        {
            "id": project["id"],
            "name": project["name"],
            "description": project["description"],
            "group": project["group"],
            "tabs": [tab["name"] for tab in project["tabs"]],
            "active": project["name"].casefold() in active_by_name,
            "workspace_id": active_by_name.get(project["name"].casefold(), ""),
            "has_agent": active_by_name.get(project["name"].casefold(), "") in agent_workspaces,
            "agent": agent_by_workspace.get(
                active_by_name.get(project["name"].casefold(), ""), "",
            ),
        }
        for project in projects
    ]


def list_workspaces(runner) -> list[dict]:
    ok, output = runner("workspace", "list")
    if not ok:
        raise ProjectCatalogError("Could not read active Herdr workspaces")
    result = _json_result(output)
    workspaces = result.get("workspaces", [])
    if not isinstance(workspaces, list):
        raise ProjectCatalogError("Herdr returned an invalid workspace list")
    return [workspace for workspace in workspaces if isinstance(workspace, dict)]


def activate_project(
    project_id: str, agent_kind: str, projects_dir: Path, runner, sleep=time.sleep,
    agent_choices: list[dict] | None = None, start_new: bool = False,
) -> dict:
    if not PROJECT_ID_RE.fullmatch(project_id):
        raise ProjectCatalogError("Unknown project")
    projects = load_projects(projects_dir)
    project = next((item for item in projects if item["id"] == project_id), None)
    if not project:
        raise ProjectCatalogError("Unknown project")
    choices = agent_choices if agent_choices is not None else available_agents(runner)
    selected_agent = next((agent for agent in choices if agent["id"] == agent_kind), None)

    for workspace in list_workspaces(runner):
        if str(workspace.get("label", "")).casefold() != project["name"].casefold():
            continue
        workspace_id = str(workspace.get("workspace_id", ""))
        if not workspace_id or not _run_ok(runner, "workspace", "focus", workspace_id):
            raise ProjectCatalogError("Could not focus the active project")
        if start_new:
            if not selected_agent:
                raise ProjectCatalogError("Selected agent is unavailable")
            working_dir = _project_working_dir(project)
            existing_agents = _list_agents(runner)
            agent_name = _unique_agent_name(
                _agent_name(project["id"], selected_agent["id"]),
                {str(agent.get("name", "")) for agent in existing_agents},
            )
            pane_id = _start_agent_tab(
                project, selected_agent, workspace_id, working_dir, runner, sleep,
                agent_name=agent_name,
            )
            return {
                "workspace_id": workspace_id, "already_active": True,
                "agent_started": True, "started_new": True,
                "pane_id": pane_id, "agent": agent_kind,
            }
        existing_agent = _workspace_agent(workspace_id, runner)
        if existing_agent:
            pane_id = str(existing_agent.get("pane_id", ""))
            if pane_id:
                _run_ok(runner, "agent", "focus", pane_id)
            return {
                "workspace_id": workspace_id, "already_active": True,
                "agent_started": False, "started_new": False, "pane_id": pane_id,
                "agent": str(existing_agent.get("agent", "")),
            }
        if not selected_agent:
            raise ProjectCatalogError("Selected agent is unavailable")
        working_dir = _project_working_dir(project)
        pane_id = _start_agent_tab(
            project, selected_agent, workspace_id, working_dir, runner, sleep,
        )
        return {
            "workspace_id": workspace_id, "already_active": True,
            "agent_started": True, "started_new": False,
            "pane_id": pane_id, "agent": agent_kind,
        }

    if not selected_agent:
        raise ProjectCatalogError("Selected agent is unavailable")
    working_dir = _project_working_dir(project)

    ok, output = runner(
        "workspace", "create", "--cwd", str(working_dir),
        "--label", project["name"], "--focus",
    )
    if not ok:
        raise ProjectCatalogError("Could not create the project workspace")
    result = _json_result(output)
    workspace_id = _nested_id(result, "workspace", "workspace_id")
    root_tab_id = _nested_id(result, "tab", "tab_id")
    root_pane_id = _nested_id(result, "root_pane", "pane_id")
    if not all((workspace_id, root_tab_id, root_pane_id)):
        raise ProjectCatalogError("Herdr returned an invalid workspace result")

    try:
        startup_commands = _layout_project(
            project, workspace_id, root_tab_id, root_pane_id, runner,
        )
        for pane_id, command in startup_commands:
            _wait_for_pane_ready(pane_id, runner, sleep)
            if not _run_ok(runner, "pane", "run", pane_id, command):
                raise ProjectCatalogError("Could not start a configured project command")
        agent_pane_id = _start_agent_tab(
            project, selected_agent, workspace_id, working_dir, runner, sleep,
        )
    except Exception:
        runner("workspace", "close", workspace_id)
        raise
    return {
        "workspace_id": workspace_id, "already_active": False,
        "agent_started": True, "started_new": False,
        "pane_id": agent_pane_id, "agent": agent_kind,
    }


def close_project(project_id: str, projects_dir: Path, runner) -> dict:
    """Close the Herdr workspace owned by a known project template.

    The caller only supplies a catalog project id; the workspace is resolved and
    allowlisted here so a client can never close an arbitrary workspace.
    """
    if not PROJECT_ID_RE.fullmatch(project_id):
        raise ProjectCatalogError("Unknown project")
    projects = load_projects(projects_dir)
    project = next((item for item in projects if item["id"] == project_id), None)
    if not project:
        raise ProjectCatalogError("Unknown project")
    workspace_id = next(
        (
            str(workspace.get("workspace_id", ""))
            for workspace in list_workspaces(runner)
            if str(workspace.get("label", "")).casefold() == project["name"].casefold()
            and workspace.get("workspace_id")
        ),
        "",
    )
    if not workspace_id:
        return {"workspace_id": "", "closed": False, "already_closed": True}
    if not _run_ok(runner, "workspace", "close", workspace_id):
        raise ProjectCatalogError("Could not close the project workspace")
    return {"workspace_id": workspace_id, "closed": True, "already_closed": False}


def _list_agents(runner) -> list[dict]:
    ok, output = runner("agent", "list")
    if not ok:
        raise ProjectCatalogError("Could not inspect active Herdr agents")
    agents = _json_result(output).get("agents", [])
    if not isinstance(agents, list):
        raise ProjectCatalogError("Herdr returned an invalid agent list")
    return [agent for agent in agents if isinstance(agent, dict)]


def _workspace_agent(workspace_id: str, runner) -> dict | None:
    return next(
        (
            agent for agent in _list_agents(runner)
            if agent.get("workspace_id") == workspace_id
        ),
        None,
    )


def _project_working_dir(project: dict) -> Path:
    working_dir = Path(os.path.expandvars(os.path.expanduser(project["working_dir"]))).resolve()
    if not working_dir.is_dir():
        raise ProjectCatalogError("Project working directory does not exist")
    return working_dir


def _start_agent_tab(
    project, selected_agent, workspace_id, working_dir, runner, sleep, agent_name="",
):
    ok, output = runner(
        "tab", "create", "--workspace", workspace_id, "--cwd", str(working_dir),
        "--label", selected_agent["label"], "--focus",
    )
    if not ok:
        raise ProjectCatalogError("Could not create the agent tab")
    result = _json_result(output)
    tab_id = _nested_id(result, "tab", "tab_id")
    pane_id = _nested_id(result, "root_pane", "pane_id")
    if not tab_id or not pane_id:
        raise ProjectCatalogError("Herdr returned an invalid agent tab")
    try:
        _wait_for_pane_ready(pane_id, runner, sleep)
        if not _run_ok(
            runner, "agent", "start", agent_name or _agent_name(project["id"]),
            "--kind", selected_agent["id"], "--pane", pane_id, "--timeout", "12000",
        ):
            raise ProjectCatalogError(f"Could not start {selected_agent['label']}")
    except Exception:
        runner("tab", "close", tab_id)
        raise
    return pane_id


def _agent_name(project_id: str, agent_kind: str = "") -> str:
    raw = f"{project_id}-{agent_kind}" if agent_kind else project_id
    name = re.sub(r"[^a-z0-9_-]", "-", raw.casefold())
    if not name or not name[0].isalpha():
        name = f"p-{name}"
    return name[:32]


def _unique_agent_name(base: str, existing: set[str]) -> str:
    """Keep an additional agent from colliding with one already running."""
    if base not in existing:
        return base
    for index in range(2, 100):
        suffix = f"-{index}"
        candidate = f"{base[:32 - len(suffix)]}{suffix}"
        if candidate not in existing:
            return candidate
    raise ProjectCatalogError("Too many agents are already running for this project")


def _normalize_project(project_id: str, data: dict, source: str) -> dict:
    if not isinstance(data, dict):
        raise ProjectCatalogError(f"Invalid project template: {source}")
    name = str(data.get("name", "")).strip()
    working_dir = str(data.get("working_dir", "")).strip()
    tabs = data.get("tabs", [])
    if not name or len(name) > 128 or not working_dir or len(working_dir) > 2048:
        raise ProjectCatalogError(f"Invalid project template: {source}")
    if not isinstance(tabs, list) or not 1 <= len(tabs) <= MAX_TABS:
        raise ProjectCatalogError(f"Invalid project template: {source}")
    normalized_tabs = [_normalize_tab(tab, source) for tab in tabs]
    return {
        "id": project_id,
        "name": name,
        "description": str(data.get("description", "")).strip()[:256],
        "group": str(data.get("group", "Ungrouped")).strip()[:80] or "Ungrouped",
        "working_dir": working_dir,
        "tabs": normalized_tabs,
    }


def _normalize_tab(tab: dict, source: str) -> dict:
    if not isinstance(tab, dict):
        raise ProjectCatalogError(f"Invalid project template: {source}")
    name = str(tab.get("name", "")).strip()
    command = str(tab.get("command", "")).strip()
    panes = tab.get("panes", [])
    if not name or len(name) > 128 or len(command) > 4096 or not isinstance(panes, list):
        raise ProjectCatalogError(f"Invalid project template: {source}")
    if command and panes or len(panes) > MAX_PANES_PER_TAB:
        raise ProjectCatalogError(f"Invalid project template: {source}")
    if not panes:
        panes = [{"command": command, "split": ""}]
    normalized_panes = []
    for index, pane in enumerate(panes):
        if not isinstance(pane, dict):
            raise ProjectCatalogError(f"Invalid project template: {source}")
        split = str(pane.get("split", "")).strip() or ("" if index == 0 else "down")
        pane_command = str(pane.get("command", "")).strip()
        if split not in {"", "down", "right"} or len(pane_command) > 4096:
            raise ProjectCatalogError(f"Invalid project template: {source}")
        normalized_panes.append({"command": pane_command, "split": split})
    return {"name": name, "panes": normalized_panes}


def _layout_project(project, workspace_id, root_tab_id, root_pane_id, runner):
    startups = []
    for tab_index, tab in enumerate(project["tabs"]):
        if tab_index == 0:
            if not _run_ok(runner, "tab", "rename", root_tab_id, tab["name"]):
                raise ProjectCatalogError("Could not name the first project tab")
            tab_root = root_pane_id
        else:
            ok, output = runner(
                "tab", "create", "--workspace", workspace_id,
                "--label", tab["name"], "--no-focus",
            )
            if not ok:
                raise ProjectCatalogError("Could not create a configured project tab")
            tab_root = _nested_id(_json_result(output), "root_pane", "pane_id")
            if not tab_root:
                raise ProjectCatalogError("Herdr returned an invalid tab result")

        previous_pane = tab_root
        for pane_index, pane in enumerate(tab["panes"]):
            pane_id = tab_root
            if pane_index > 0:
                ok, output = runner(
                    "pane", "split", previous_pane,
                    "--direction", pane["split"], "--no-focus",
                )
                if not ok:
                    raise ProjectCatalogError("Could not create a configured project pane")
                split_result = _json_result(output)
                pane_id = (
                    _nested_id(split_result, "pane", "pane_id")
                    or _nested_id(split_result, "root_pane", "pane_id")
                    or str(split_result.get("pane_id", ""))
                )
                if not pane_id:
                    raise ProjectCatalogError("Herdr returned an invalid pane result")
            if pane["command"]:
                startups.append((pane_id, pane["command"]))
            previous_pane = pane_id
    return startups


def _wait_for_pane_ready(pane_id, runner, sleep, timeout_seconds=5):
    deadline = time.monotonic() + timeout_seconds
    stable_checks = 0
    while time.monotonic() < deadline:
        ok, output = runner("pane", "process-info", "--pane", pane_id)
        if ok:
            process_info = _json_result(output).get("process_info", {})
            shell_pid = process_info.get("shell_pid") if isinstance(process_info, dict) else None
            foreground_pgid = (
                process_info.get("foreground_process_group_id")
                if isinstance(process_info, dict) else None
            )
            if shell_pid and shell_pid == foreground_pgid:
                stable_checks += 1
                if stable_checks >= 10:
                    return
            else:
                stable_checks = 0
        sleep(0.05)
    raise ProjectCatalogError("Agent shell did not become ready")


def _json_result(output: str) -> dict:
    try:
        data = json.loads(output)
        result = data.get("result")
    except (json.JSONDecodeError, AttributeError) as exc:
        raise ProjectCatalogError("Herdr returned an invalid response") from exc
    if not isinstance(result, dict):
        raise ProjectCatalogError("Herdr returned an invalid response")
    return result


def _nested_id(result: dict, object_name: str, field_name: str) -> str:
    value = result.get(object_name, {})
    return str(value.get(field_name, "")) if isinstance(value, dict) else ""


def _run_ok(runner, *args) -> bool:
    ok, _ = runner(*args)
    return ok
