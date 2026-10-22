"""Validate the independent MES-07 synthetic oracle; no product code is imported."""

from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


def feedback_rows(source: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = list(source.get("feedback", []))
    for batch in source.get("feedback_batches", []):
        for offset in range(batch["count"]):
            rows.append({**batch, "id": batch["id_start"] + offset})
    return rows


def check_case(case: dict[str, Any]) -> None:
    source, expected = case["input"], case["expected"]
    assert case["split"] in {"dev", "holdout"}
    assert isinstance(case["case_id"], str) and case["case_id"]
    assert expected["process_metric"] is None
    rows = feedback_rows(source)
    ids = [row["id"] for row in rows]
    assert len(ids) == len(set(ids)), case["case_id"]
    for trace_id in expected["trace_ids"]:
        assert trace_id in ids, case["case_id"]
    if not expected["available"]:
        assert expected["unavailable_reason"] in {
            "PERMISSION_DENIED", "INCONSISTENT_OBSERVATION", "SOURCE_INCOMPLETE"
        }
        for field in ("workorder_produced_fact", "remaining_raw", "ratio_percent", "process_finished_sums"):
            assert expected[field] is None, case["case_id"]
        return

    workorder = source["workorder"]
    plan = Decimal(workorder["quantity"]) if workorder["quantity"] is not None else None
    produced = Decimal(workorder["quantityProduced"]) if workorder["quantityProduced"] is not None else None
    assert expected["workorder_produced_fact"] == workorder["quantityProduced"]
    if plan is None or produced is None:
        assert expected["remaining_raw"] is None
        assert expected["ratio_percent"] is None
    else:
        assert Decimal(expected["remaining_raw"]) == plan - produced
        if plan == 0:
            assert expected["ratio_percent"] is None
            assert expected["ratio_reason"] == "ZERO_DENOMINATOR"
        else:
            ratio = (produced * 100 / plan).quantize(Decimal("0.000000000000000001"), rounding=ROUND_HALF_UP)
            assert Decimal(expected["ratio_percent"]) == ratio, case["case_id"]

    process_totals: defaultdict[str, Decimal] = defaultdict(Decimal)
    for task in source["tasks"]:
        process_totals[str(task["process_id"])] += Decimal(0)
    for row in rows:
        if row["status"] == "FINISHED":
            process_totals[str(row["process_id"])] += Decimal(row["quantityFeedback"])
    assert {key: Decimal(value) for key, value in expected["process_finished_sums"].items()} == dict(process_totals), case["case_id"]
    if source.get("pages_complete"):
        assert len(rows) == source["feedback_total"], case["case_id"]


def main() -> None:
    path = Path(__file__).with_name("cases.json")
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    ids = [case["case_id"] for case in cases]
    assert len(ids) == len(set(ids))
    assert len(cases) >= 12
    assert {case["split"] for case in cases} == {"dev", "holdout"}
    for case in cases:
        check_case(case)
    print(f"PASS: {len(cases)} synthetic cases; unique IDs, splits and arithmetic verified")


if __name__ == "__main__":
    main()
