"""Two-process MES-10 proposal and confirmation recovery check."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from typing import cast
from typing import Mapping

from ktg_agent.core.messages import JsonObject
from ktg_agent.ktg.client import KTGClient
from ktg_agent.ktg.workorders import PreviewStore
from ktg_agent.providers.base import ModelConfig, Provider
from ktg_agent.service import QueryService


def _connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS mes_operations (
        operation_id TEXT PRIMARY KEY, action TEXT NOT NULL, digest TEXT NOT NULL,
        outcome TEXT NOT NULL, workorder_id INTEGER NOT NULL, draft_json TEXT NOT NULL
    )""")
    db.commit()
    return db


def prepare(store_path: str, mes_path: str) -> None:
    request = cast(JsonObject, {
        "workorderCode": "WO-MES10-RESTART", "workorderName": "Restart recovery",
        "workorderType": "SELF", "orderSource": "MANUAL", "productId": 11,
        "quantity": "100", "requestDate": "2026-10-31", "parentId": 0,
    })
    preview = cast(JsonObject, {"kind": "workorder_draft_preview", "workorderCode": request["workorderCode"],
                               "quantity": request["quantity"], "changes": []})
    operation_id = PreviewStore(path=store_path).create(
        user_key="id:7", action="create", request=request, preview=preview,
    )
    with _connect(mes_path):
        pass
    print(json.dumps({"pid": __import__("os").getpid(), "operationId": operation_id}))


def confirm(store_path: str, mes_path: str, operation_id: str) -> None:
    def client_factory(authorization: str) -> KTGClient:
        if authorization != "Bearer mes10-isolated-token":
            raise AssertionError("unexpected auth token")

        async def read(url: str, _headers: Mapping[str, str], _params: Mapping[str, object], _timeout: float) -> object:
            if url.endswith("/getInfo"):
                return {"code": 200, "user": {"userId": 7, "userName": "mes10-isolated"},
                        "permissions": ["mes:pro:workorder:add", "mes:pro:workorder:query",
                                        "mes:pro:workorder:list", "mes:md:bom:list",
                                        "mes:pro:workorderbom:list"]}
            if "/mes/pro/workorder/operations/" in url:
                op_id = url.rsplit("/", 1)[-1]
                with _connect(mes_path) as db:
                    row = db.execute("SELECT * FROM mes_operations WHERE operation_id=?", (op_id,)).fetchone()
                if row is None:
                    return {"code": 404, "errorCode": "OPERATION_NOT_FOUND"}
                return {"code": 200, "data": {
                    "operationId": row["operation_id"], "action": row["action"],
                    "requestDigest": row["digest"], "expectedVersion": None,
                    "outcome": row["outcome"], "workorderId": row["workorder_id"], "workorderCode": "WO-MES10-RESTART",
                }}
            if url.endswith("/mes/pro/workorder/51"):
                with _connect(mes_path) as db:
                    row = db.execute("SELECT draft_json FROM mes_operations WHERE workorder_id=51").fetchone()
                if row is None:
                    return {"code": 404}
                return {"code": 200, "data": {
                    **json.loads(row["draft_json"]), "workorderId": 51, "version": 0,
                    "status": "PREPARE", "productName": "Product A", "unitOfMeasure": "piece",
                }}
            if url.endswith("/mes/md/bom/list") or url.endswith("/mes/pro/workorderbom/list"):
                return {"code": 200, "total": 0, "rows": []}
            raise AssertionError(url)

        async def write(method: str, url: str, _headers: Mapping[str, str], body: Mapping[str, object], _timeout: float) -> dict[str, object]:
            if method != "POST" or not url.endswith("/mes/pro/workorder/operations"):
                raise AssertionError((method, url))
            draft = json.dumps(body["draft"], ensure_ascii=False, separators=(",", ":"))
            with _connect(mes_path) as db:
                db.execute("INSERT OR IGNORE INTO mes_operations VALUES (?, ?, ?, 'succeeded', 51, ?)",
                           (body["operationId"], body["action"], body["requestDigest"], draft))
            return {"code": 200}

        return KTGClient(base_url="http://mes10-isolated.invalid", authorization=authorization,
                         transport=read, write_transport=write)

    service = QueryService(provider=cast(Provider, object()), model_config=ModelConfig("unused", 16),
                           ktg_client_factory=client_factory,
                           workorder_store=PreviewStore(path=store_path))
    response = asyncio.run(service.confirm(preview_id=operation_id,
                                           authorization="Bearer mes10-isolated-token"))
    result = response["result"]
    if not isinstance(result, dict):
        raise AssertionError("confirmation result missing")
    outcome = result["outcome"]
    with _connect(mes_path) as db:
        count = db.execute("SELECT COUNT(*) FROM mes_operations WHERE operation_id=?", (operation_id,)).fetchone()[0]
    print(json.dumps({"pid": __import__("os").getpid(), "operationId": operation_id,
                      "outcome": outcome, "receiptCount": count}))


if __name__ == "__main__":
    mode, store_path, mes_path, *rest = sys.argv[1:]
    if mode == "prepare":
        prepare(store_path, mes_path)
    elif mode == "confirm":
        confirm(store_path, mes_path, rest[0])
    else:
        raise ValueError(mode)
