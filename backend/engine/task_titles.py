"""
AgentOS — Task Title Derivation

One shared implementation for the human-readable task title shown in the
timeline, DAG nodes, and details panel. Used by the workflow router,
the /intent endpoint, and the scheduled-run factory so every task
creation path produces the same label.
"""

from typing import Any


def task_title(t_def: Any) -> str:
    """Human-readable task title: '<tool|agent>: <first meaningful input>'."""
    label = t_def.tool or t_def.agent
    data = getattr(t_def, "input_data", None) or {}
    for key in ("url", "source", "prompt", "goal", "subject", "to"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            detail = val.strip().replace("\n", " ")
            return f"{label}: {detail[:80]}" + ("…" if len(detail) > 80 else "")
    return label
