import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4


def test_proposal_is_confirmed_after_separate_agent_process_restart() -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "mes10_restart_worker.py"
    store_path = Path(".runtime") / f"mes10-agent-{uuid4()}.sqlite3"
    mes_path = Path(".runtime") / f"mes10-mes-{uuid4()}.sqlite3"
    try:
        prepared = subprocess.run(
            [sys.executable, str(fixture), "prepare", str(store_path.resolve()), str(mes_path.resolve())],
            check=True, capture_output=True, text=True,
        )
        prep = json.loads(prepared.stdout)
        resumed = subprocess.run(
            [sys.executable, str(fixture), "confirm", str(store_path.resolve()),
             str(mes_path.resolve()), prep["operationId"]],
            check=True, capture_output=True, text=True,
        )
        result = json.loads(resumed.stdout)
        assert prep["pid"] != result["pid"]
        assert result["operationId"] == prep["operationId"]
        assert result["outcome"] == "succeeded"
        assert result["receiptCount"] == 1
        queried = subprocess.run(
            [sys.executable, str(fixture), "confirm", str(store_path.resolve()),
             str(mes_path.resolve()), prep["operationId"]],
            check=True, capture_output=True, text=True,
        )
        replay = json.loads(queried.stdout)
        assert replay["pid"] not in {prep["pid"], result["pid"]}
        assert replay["operationId"] == prep["operationId"]
        assert replay["outcome"] == "succeeded"
        assert replay["receiptCount"] == 1
        print(f"MES10_RESTART pid_before={prep['pid']} pid_confirm={result['pid']} "
              f"pid_query={replay['pid']} operationId={result['operationId']} "
              f"outcome={result['outcome']} receiptCount={replay['receiptCount']}")
    finally:
        store_path.unlink(missing_ok=True)
        mes_path.unlink(missing_ok=True)
        for base in (store_path, mes_path):
            Path(str(base) + "-wal").unlink(missing_ok=True)
            Path(str(base) + "-shm").unlink(missing_ok=True)
