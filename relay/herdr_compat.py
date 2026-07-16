"""Compatibility adapters for Herdr 0.7+ CLI JSON responses."""

from __future__ import annotations

import json
import os


def parse_pane_list(raw: str, remote=None) -> list[dict]:
    """Map Herdr pane-list JSON to relay agents, ignoring non-agent panes."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    result = data.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("panes"), list):
        return []

    agents = []
    host_label = remote or "local"
    for pane in result["panes"]:
        if not isinstance(pane, dict) or not pane.get("agent") or not pane.get("pane_id"):
            continue
        cwd = pane.get("cwd", "")
        agents.append({
            "pane_id": pane["pane_id"],
            "agent": pane.get("agent", ""),
            "label": pane.get("label", ""),
            "status": pane.get("agent_status", "unknown"),
            "cwd": cwd,
            "project": os.path.basename(cwd),
            "host": host_label,
            "remote": remote,
            "workspace_id": pane.get("workspace_id", ""),
            "tab_id": pane.get("tab_id", ""),
        })
    return agents
