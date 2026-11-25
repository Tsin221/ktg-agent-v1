"""Read-only regression of exact identifier resolution on the formal synthetic demo."""
from __future__ import annotations

import asyncio
import json
import runpy
from pathlib import Path

from ktg_agent.ktg.business_reference import build_business_reference_tool
from ktg_agent.ktg.client import KTGClient
from ktg_agent.tools import ToolContext

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:18090"


async def verify() -> None:
    demo = runpy.run_path(str(ROOT / "scripts/verify-mes-demo-agent.py"))
    account = demo["login"](BASE)
    client = KTGClient(base_url=BASE, authorization=account)
    definition = build_business_reference_tool()
    cases = {"LAMPBAT-00000023": "sn", "WO-LAMP-20261124-RETAIL": "workorder",
             "CELL-18650-2200": "item", "CELL-20261124-RETAIL-MORNING": "batch",
             "UNRECOGNIZED-REF-090": None}
    results = []
    for code, expected in cases.items():
        result = await definition.execute(ToolContext("reference-verify", code, ktg_client=client), {"code": code})
        details = result.details
        assert details is not None
        assert details["coverage"] == "complete", details
        matches = details["matches"]
        assert isinstance(matches, list)
        if expected is None:
            assert not matches and details["resolution"] == "not_found", details
        else:
            assert len(matches) == 1 and matches[0]["type"] == expected, details
            if expected == "sn":
                assert matches[0]["workorder_code"] == "WO-LAMP-20261124-RETAIL", details
        results.append(details)
        print(code, details["resolution"], expected, flush=True)
    evidence = ROOT / ".runtime/mes-demo/agent/business-reference-verified.json"
    evidence.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Five reference cases verified against real local MES; no model request or business write.")


if __name__ == "__main__":
    asyncio.run(verify())
