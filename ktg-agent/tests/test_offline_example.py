from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_fake_agent_example_runs_without_external_services() -> None:
    project_root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, str(project_root / "examples" / "fake_agent.py")],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )

    assert completed.stderr == ""
    assert completed.stdout.splitlines()[:3] == [
        "final: 仓库 WH-A 的可用库存为 8。",
        "provider_requests: 2",
        "tool_calls: 1",
    ]
    assert completed.stdout.splitlines()[3].endswith("turn_end -> agent_end")
