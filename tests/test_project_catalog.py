import json
import tempfile
import unittest
from pathlib import Path

from project_catalog import (
    ProjectCatalogError,
    activate_project,
    available_agents,
    close_project,
    load_projects,
    public_projects,
)


def response(result):
    return json.dumps({"id": "test", "result": result})


class FakeRunner:
    def __init__(self, workspaces=None, agents=None, failing=()):
        self.calls = []
        self.workspaces = workspaces or []
        self.agents = agents or []
        self.failing = set(failing)
        self.process_info_checks = 0

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] in self.failing:
            return False, ""
        if args == ("workspace", "list"):
            return True, response({"type": "workspace_list", "workspaces": self.workspaces})
        if args == ("agent", "list"):
            return True, response({"type": "agent_list", "agents": self.agents})
        if args[:2] == ("workspace", "create"):
            return True, response({
                "type": "workspace_created",
                "workspace": {"workspace_id": "w-new"},
                "tab": {"tab_id": "t-root"},
                "root_pane": {"pane_id": "p-root"},
            })
        if args[:2] == ("tab", "create"):
            if "--focus" in args:
                return True, response({
                    "type": "tab_created",
                    "tab": {"tab_id": "t-agent"},
                    "root_pane": {"pane_id": "p-agent"},
                })
            return True, response({
                "type": "tab_created",
                "tab": {"tab_id": "t-second"},
                "root_pane": {"pane_id": "p-second"},
            })
        if args[:2] == ("pane", "split"):
            return True, response({"type": "pane_info", "pane": {"pane_id": "p-split"}})
        if args[:2] == ("pane", "process-info"):
            self.process_info_checks += 1
            return True, response({
                "type": "pane_process_info",
                "process_info": {
                    "shell_pid": 123, "foreground_process_group_id": 123,
                },
            })
        return True, ""


class ProjectCatalogTests(unittest.TestCase):
    AGENTS = [{"id": "codex", "label": "Codex"}]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.projects_dir = Path(self.temp.name)
        self.working_dir = self.projects_dir / "repo"
        self.working_dir.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def write_project(self, name="Alpha"):
        (self.projects_dir / "alpha.toml").write_text(
            f'''name = "{name}"
description = "Alpha workspace"
group = "Personal"
working_dir = "{self.working_dir}"

[[tabs]]
name = "terminal"

[[tabs]]
name = "tools"

[[tabs.panes]]
command = "fresh -a"

[[tabs.panes]]
command = "lazygit"
split = "right"
''',
            encoding="utf-8",
        )

    def test_catalog_exposes_display_metadata_without_paths_or_commands(self):
        self.write_project()
        projects = load_projects(self.projects_dir)
        public = public_projects(
            projects,
            [{"label": "Alpha", "workspace_id": "w1"}],
            [{"agent": "codex", "workspace_id": "w1"}],
        )
        self.assertEqual(public, [{
            "id": "alpha", "name": "Alpha", "description": "Alpha workspace",
            "group": "Personal", "tabs": ["terminal", "tools"],
            "active": True, "workspace_id": "w1", "has_agent": True,
            "agent": "codex",
        }])
        self.assertNotIn("working_dir", public[0])
        self.assertNotIn("command", json.dumps(public[0]))

    def test_agent_choices_only_expose_installed_allowlisted_agents(self):
        installed = {"codex", "opencode"}
        choices = available_agents(
            lambda *args: (True, "codex: current (v7)\nopencode: not installed\n"),
            lambda executable: f"/bin/{executable}" if executable in installed else None,
            executable_check=lambda _: False,
        )
        self.assertEqual(choices, [{"id": "codex", "label": "Codex"}])

    def test_active_project_with_agent_is_focused_without_creating_a_duplicate(self):
        self.write_project()
        runner = FakeRunner(
            [{"label": "Alpha", "workspace_id": "w-existing"}],
            [{"agent": "claude", "workspace_id": "w-existing", "pane_id": "p-live"}],
        )
        result = activate_project(
            "alpha", "codex", self.projects_dir, runner, agent_choices=self.AGENTS,
        )
        self.assertEqual(result, {
            "workspace_id": "w-existing", "already_active": True,
            "agent_started": False, "started_new": False, "pane_id": "p-live",
            "agent": "claude",
        })
        self.assertIn(("workspace", "focus", "w-existing"), runner.calls)
        self.assertIn(("agent", "focus", "p-live"), runner.calls)
        self.assertFalse(any(call[:2] == ("workspace", "create") for call in runner.calls))
        self.assertFalse(any(call[:2] == ("agent", "start") for call in runner.calls))

    def test_active_project_without_agent_starts_selected_agent(self):
        self.write_project()
        runner = FakeRunner([{"label": "Alpha", "workspace_id": "w-existing"}])
        result = activate_project(
            "alpha", "codex", self.projects_dir, runner,
            sleep=lambda _: None, agent_choices=self.AGENTS,
        )
        self.assertEqual(result, {
            "workspace_id": "w-existing", "already_active": True,
            "agent_started": True, "started_new": False, "pane_id": "p-agent",
            "agent": "codex",
        })
        self.assertIn((
            "agent", "start", "alpha", "--kind", "codex", "--pane", "p-agent",
            "--timeout", "12000",
        ), runner.calls)
        self.assertGreaterEqual(runner.process_info_checks, 10)

    def test_active_agent_can_resume_even_if_its_executable_is_no_longer_available(self):
        self.write_project()
        runner = FakeRunner(
            [{"label": "Alpha", "workspace_id": "w-existing"}],
            [{"agent": "gemini", "workspace_id": "w-existing", "pane_id": "p-live"}],
        )
        result = activate_project(
            "alpha", "gemini", self.projects_dir, runner, agent_choices=self.AGENTS,
        )
        self.assertFalse(result["agent_started"])
        self.assertEqual(result["agent"], "gemini")
        self.assertIn(("agent", "focus", "p-live"), runner.calls)

    def test_inactive_project_builds_declared_tabs_panes_and_commands(self):
        self.write_project()
        runner = FakeRunner()
        result = activate_project(
            "alpha", "codex", self.projects_dir, runner,
            sleep=lambda _: None, agent_choices=self.AGENTS,
        )
        self.assertEqual(result, {
            "workspace_id": "w-new", "already_active": False,
            "agent_started": True, "started_new": False, "pane_id": "p-agent",
            "agent": "codex",
        })
        self.assertIn(("tab", "rename", "t-root", "terminal"), runner.calls)
        self.assertTrue(any(call[:2] == ("tab", "create") for call in runner.calls))
        self.assertTrue(any(call[:2] == ("pane", "split") for call in runner.calls))
        self.assertIn(("pane", "run", "p-second", "fresh -a"), runner.calls)
        self.assertIn(("pane", "run", "p-split", "lazygit"), runner.calls)
        self.assertIn((
            "agent", "start", "alpha", "--kind", "codex", "--pane", "p-agent",
            "--timeout", "12000",
        ), runner.calls)

    def test_missing_working_directory_is_rejected_before_creation(self):
        self.write_project()
        self.working_dir.rmdir()
        runner = FakeRunner()
        with self.assertRaisesRegex(ProjectCatalogError, "working directory"):
            activate_project(
                "alpha", "codex", self.projects_dir, runner, agent_choices=self.AGENTS,
            )
        self.assertFalse(any(call[:2] == ("workspace", "create") for call in runner.calls))

    def test_project_id_cannot_escape_the_catalog(self):
        self.write_project()
        with self.assertRaisesRegex(ProjectCatalogError, "Unknown project"):
            activate_project(
                "../alpha", "codex", self.projects_dir, FakeRunner(),
                agent_choices=self.AGENTS,
            )

    def test_starting_another_agent_adds_a_tab_without_touching_the_running_one(self):
        self.write_project()
        runner = FakeRunner(
            [{"label": "Alpha", "workspace_id": "w-existing"}],
            [{
                "name": "alpha", "agent": "claude",
                "workspace_id": "w-existing", "pane_id": "p-live",
            }],
        )
        result = activate_project(
            "alpha", "codex", self.projects_dir, runner, sleep=lambda _: None,
            agent_choices=self.AGENTS, start_new=True,
        )
        self.assertEqual(result, {
            "workspace_id": "w-existing", "already_active": True,
            "agent_started": True, "started_new": True, "pane_id": "p-agent",
            "agent": "codex",
        })
        self.assertIn((
            "tab", "create", "--workspace", "w-existing", "--cwd", str(self.working_dir),
            "--label", "Codex", "--focus",
        ), runner.calls)
        self.assertIn((
            "agent", "start", "alpha-codex", "--kind", "codex", "--pane", "p-agent",
            "--timeout", "12000",
        ), runner.calls)
        for call in runner.calls:
            self.assertNotIn(call[:2], {("agent", "stop"), ("pane", "kill"), ("tab", "close")})
            self.assertNotEqual(call[:2], ("workspace", "close"))
            self.assertNotIn("p-live", call)

    def test_additional_agent_name_does_not_collide_with_a_running_agent(self):
        self.write_project()
        runner = FakeRunner(
            [{"label": "Alpha", "workspace_id": "w-existing"}],
            [{"name": "alpha-codex", "agent": "codex", "workspace_id": "w-existing"}],
        )
        activate_project(
            "alpha", "codex", self.projects_dir, runner, sleep=lambda _: None,
            agent_choices=self.AGENTS, start_new=True,
        )
        self.assertIn((
            "agent", "start", "alpha-codex-2", "--kind", "codex", "--pane", "p-agent",
            "--timeout", "12000",
        ), runner.calls)

    def test_starting_another_agent_rejects_an_uninstalled_agent(self):
        self.write_project()
        runner = FakeRunner([{"label": "Alpha", "workspace_id": "w-existing"}])
        with self.assertRaisesRegex(ProjectCatalogError, "unavailable"):
            activate_project(
                "alpha", "gemini", self.projects_dir, runner,
                agent_choices=self.AGENTS, start_new=True,
            )
        self.assertFalse(any(call[:2] == ("tab", "create") for call in runner.calls))

    def test_close_project_closes_the_workspace_owned_by_the_template(self):
        self.write_project()
        runner = FakeRunner([
            {"label": "Unrelated", "workspace_id": "w-other"},
            {"label": "Alpha", "workspace_id": "w-existing"},
        ])
        result = close_project("alpha", self.projects_dir, runner)
        self.assertEqual(result, {
            "workspace_id": "w-existing", "closed": True, "already_closed": False,
        })
        self.assertIn(("workspace", "close", "w-existing"), runner.calls)
        self.assertNotIn(("workspace", "close", "w-other"), runner.calls)

    def test_close_project_is_graceful_when_the_workspace_is_already_gone(self):
        self.write_project()
        runner = FakeRunner([{"label": "Unrelated", "workspace_id": "w-other"}])
        result = close_project("alpha", self.projects_dir, runner)
        self.assertEqual(result, {
            "workspace_id": "", "closed": False, "already_closed": True,
        })
        self.assertFalse(any(call[:2] == ("workspace", "close") for call in runner.calls))

    def test_close_project_refuses_projects_outside_the_catalog(self):
        self.write_project()
        for unknown in ("../alpha", "beta", "alpha/../beta"):
            runner = FakeRunner([{"label": "Alpha", "workspace_id": "w-existing"}])
            with self.assertRaisesRegex(ProjectCatalogError, "Unknown project"):
                close_project(unknown, self.projects_dir, runner)
            self.assertFalse(any(call[:2] == ("workspace", "close") for call in runner.calls))

    def test_close_project_reports_a_failed_workspace_close(self):
        self.write_project()
        runner = FakeRunner(
            [{"label": "Alpha", "workspace_id": "w-existing"}],
            failing=[("workspace", "close")],
        )
        with self.assertRaisesRegex(ProjectCatalogError, "Could not close"):
            close_project("alpha", self.projects_dir, runner)

    def test_unavailable_agent_is_rejected_before_workspace_changes(self):
        self.write_project()
        runner = FakeRunner()
        with self.assertRaisesRegex(ProjectCatalogError, "unavailable"):
            activate_project(
                "alpha", "unknown", self.projects_dir, runner, agent_choices=self.AGENTS,
            )
        self.assertEqual(runner.calls, [("workspace", "list")])


if __name__ == "__main__":
    unittest.main()
