from __future__ import annotations

import asyncio
from typing import cast

import pytest

from ktg_agent.core.messages import JsonObject
from ktg_agent.exploration.compute import ComputeRunner
from ktg_agent.exploration.compute_worker import run_job
from ktg_agent.ktg.client import KTGError


def test_worker_protocol_executes_analysis_preserving_decimal_and_source_types() -> None:
    datasets: JsonObject = {"repair": {"columns": [{"name": "cost", "type": "decimal"}],
        "rows": [["0.10"], ["0.20"]], "coverage": "complete", "source": "test"}}
    code = '''from decimal import Decimal
def analyze(datasets):
    total = sum(Decimal(row[0]) for row in datasets["repair"]["rows"])
    print("diagnostic text is not the result")
    return {"summary": str(total), "tables": [{"columns": [{"name": "total", "type": "decimal"}], "rows": [[str(total)]]}], "artifacts": [], "warnings": []}
'''
    result = run_job({"datasets": datasets, "code": code})
    assert result["summary"] == "0.30"
    assert result["artifacts"] == []
    assert "diagnostic text" not in str(result)


@pytest.mark.parametrize("code", ["x=1", "def analyze(datasets): return 3", "def analyze(datasets): raise ValueError('private')"])
def test_worker_invalid_code_or_output_never_returns_a_success_result(code: str) -> None:
    with pytest.raises(ValueError):
        run_job({"datasets": {}, "code": code})


def test_worker_rejects_host_paths_in_artifact_protocol() -> None:
    code = '''def analyze(datasets):
    return {"summary": "bad", "tables": [], "warnings": [], "artifacts": [{"path": "/etc/passwd", "name": "x.csv", "mime_type": "text/csv"}]}
'''
    with pytest.raises(ValueError):
        run_job({"datasets": {}, "code": code})


def test_runner_never_falls_back_to_host_python_without_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ktg_agent.exploration.compute.shutil.which", lambda command: None)
    with pytest.raises(KTGError) as error, asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(ComputeRunner().analyze({}, "def analyze(datasets): return {}"))
    assert error.value.code == "COMPUTE_UNAVAILABLE"


def test_docker_command_enforces_isolation_and_does_not_mount_the_host() -> None:
    runner = ComputeRunner()
    args = runner.command("ktg-analysis-test")
    for flag in ("--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges:true", "--user=65532:65532", "--pids-limit=16"):
        assert flag in args
    assert not any(arg.startswith(("--volume", "--mount", "-v=")) for arg in args)
    assert "--memory=128m" in args and "--cpus=1" in args
    assert not any("docker.sock" in arg for arg in args)


def test_worker_file_protocol_contains_no_result_paths() -> None:
    # Direct worker calls verify the serialization contract only, not OS isolation.
    code = '''def analyze(datasets):
    return {"summary": "ok", "tables": [], "artifacts": [], "warnings": []}
'''
    assert run_job({"datasets": {}, "code": code}) == {
        "summary": "ok", "tables": [], "artifacts": [], "warnings": []}


@pytest.mark.parametrize("output", [
    b'{"summary":"x","tables":[3],"artifacts":[],"warnings":[]}',
    b'{"summary":"x","tables":[],"artifacts":[],"warnings":[],"secret":"x"}',
    b'{"summary":"x","tables":[{"columns":[],"rows":[[NaN]]}],"artifacts":[],"warnings":[]}',
    b'{"summary":"x","tables":[],"artifacts":[{"path":"/etc/passwd"}],"warnings":[]}',
])
def test_host_revalidates_untrusted_worker_output(output: bytes) -> None:
    with pytest.raises(KTGError) as error:
        ComputeRunner._result(output)
    assert error.value.code == "COMPUTE_INVALID_RESULT"


def test_timeout_requests_container_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    cleaned: list[str] = []
    runner = ComputeRunner(timeout_seconds=0.01)

    async def create(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        return cast(asyncio.subprocess.Process, object())

    async def exchange(proc: asyncio.subprocess.Process, payload: bytes) -> tuple[int, bytes]:
        await asyncio.sleep(10)
        return 0, b"{}"

    async def cleanup(docker: str, name: str, proc: asyncio.subprocess.Process) -> None:
        cleaned.append(name)

    monkeypatch.setattr("ktg_agent.exploration.compute.asyncio.create_subprocess_exec", create)
    monkeypatch.setattr("ktg_agent.exploration.compute._exchange", exchange)
    monkeypatch.setattr(runner, "_cleanup", cleanup)
    with pytest.raises(KTGError) as error, asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as loop:
        loop.run(runner._run("docker", {}, b"{}", None))
    assert error.value.code == "COMPUTE_TIMEOUT"
    assert len(cleaned) == 1 and cleaned[0].startswith("ktg-analysis-")
