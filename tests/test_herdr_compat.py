import json
import unittest
from herdr_compat import parse_pane_list


HERDR_074_PANE_LIST = {
    "id": "cli:pane:list",
    "result": {
        "panes": [
            {
                "agent": "codex",
                "agent_status": "working",
                "cwd": "/work/example-project",
                "focused": True,
                "foreground_cwd": "/work/example-project",
                "pane_id": "w1:p1",
                "revision": 3,
                "tab_id": "w1:t1",
                "terminal_id": "term_example",
                "workspace_id": "w1",
            },
            {
                "agent_status": "unknown",
                "cwd": "/work/example-project",
                "pane_id": "w1:p2",
                "tab_id": "w1:t1",
                "workspace_id": "w1",
            },
        ],
        "type": "pane_list",
    },
}


class HerdrCompatibilityTests(unittest.TestCase):
    def test_herdr_074_pane_list_maps_agents_and_ignores_plain_shells(self):
        agents = parse_pane_list(json.dumps(HERDR_074_PANE_LIST))
        self.assertEqual(agents, [{
            "pane_id": "w1:p1",
            "agent": "codex",
            "label": "",
            "status": "working",
            "cwd": "/work/example-project",
            "project": "example-project",
            "host": "local",
            "remote": None,
            "workspace_id": "w1",
            "tab_id": "w1:t1",
        }])

    def test_malformed_or_failed_pane_list_is_empty(self):
        for output in (
            "", "not-json", "[]", json.dumps({"result": {"panes": None}}),
            json.dumps({"result": {"panes": [{"agent": "codex"}]}}),
        ):
            with self.subTest(output=output):
                self.assertEqual(parse_pane_list(output), [])


if __name__ == "__main__":
    unittest.main()
