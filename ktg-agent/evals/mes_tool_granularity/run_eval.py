"""Offline MES-13 checks against the independent MES-07 synthetic oracle."""
from pathlib import Path
import json
import subprocess
import sys

root = Path(__file__).resolve().parents[2]
fixture = root / "tests/fixtures/mes_progress/cases.json"
validator = root / "tests/fixtures/mes_progress/validate_cases.py"
subprocess.run([sys.executable, str(validator)], check=True)
cases = json.loads(fixture.read_text(encoding="utf-8"))["cases"]
by_id = {case["case_id"]: case for case in cases}
expected_ids = ["normal_two_processes", "fractional_remaining", "unfinished_process", "trace_feedback", "large_pagination", "draft_mixed", "zero_plan", "overproduction", "missing_produced", "permission_denied", "concurrent_change", "partial_read_failure", "unit_mismatch", "correction_unknown"]
assert list(by_id) == expected_ids
# Explicit independent assertions for representative traps used by the fixed questions.
assert by_id["normal_two_processes"]["expected"]["workorder_produced_fact"] == "4"
assert by_id["trace_feedback"]["expected"]["trace_ids"] == [41, 42]
assert by_id["large_pagination"]["input"]["feedback_total"] == 25
assert by_id["permission_denied"]["expected"]["available"] is False
progress = (root / "src/ktg_agent/ktg/workorder_progress.py").read_text(encoding="utf-8")
server = (root / "src/ktg_agent/server.py").read_text(encoding="utf-8")
assert 'NAME = "ktg_pro_workorder_progress"' in progress
assert 'os.environ.get("AGENT_ENABLE_WORKORDER_PROGRESS", "false").lower() == "true"' in server
result = {"check": "offline_contract", "synthetic_case_count": len(cases), "fixed_case_ids": expected_ids, "independent_oracle": "MES-07 validate_cases.py passed", "representative_assertions": 4, "progress_default_on": False, "real_model_evaluation": "not executed", "real_mes_ids": "unavailable", "token_cost": "unknown"}
print(json.dumps(result, ensure_ascii=False, indent=2))
Path(__file__).with_name("offline-contract-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
