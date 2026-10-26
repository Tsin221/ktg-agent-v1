"""Isolated contracts for repair reads and confirmed draft writes."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping, Coroutine
import json
from pathlib import Path
import sqlite3
from time import sleep
from typing import Any, cast

import jsonschema
import pytest

from ktg_agent.core.messages import JsonObject, ToolCall
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.ktg.dv_repair import (build_dv_repair_tools, read_repair_detail,
    read_repair_lines, read_repair_list, read_repair_options, read_candidates)
from ktg_agent.ktg.dv_repair_writes import (APPLY_NAMES, PREVIEW_NAMES, _PERMS,
    DvRepairPreviewStore, build_dv_repair_write_tools, dv_repair_write_manifests)
from ktg_agent.ktg.workorders import PreviewError
from ktg_agent.server import Settings, create_service
from ktg_agent.tools.catalog import CatalogIdentity
from ktg_agent.tools.registry import ToolChannel, ToolContext, ToolDefinition, ToolResult, ToolStatus


REPAIR = {"repairId": 8, "repairCode": "R-8", "repairName": "泵维修", "machineryId": 11,
    "machineryCode": "EQ-11", "machineryName": "泵", "machineryBrand": "A", "machinerySpec": "S",
    "machineryTypeId": 3, "requireDate": "2026-10-20", "status": "PREPARE", "remark": "旧备注"}
MACH = {"machineryId": 11, "machineryCode": "EQ-11", "machineryName": "泵",
    "machineryBrand": "A", "machinerySpec": "S", "machineryTypeId": 3, "status": "REPAIR"}
SUBJECT = {"subjectId": 12, "subjectCode": "S-12", "subjectName": "检查泵", "subjectType": "REPAIR",
    "subjectContent": "检查叶轮", "subjectStandard": "无磨损", "enableFlag": "Y"}
LINE = {"lineId": 5, "repairId": 8, "subjectId": 12, "subjectCode": "S-12", "subjectName": "检查泵",
    "subjectType": "REPAIR", "subjectContent": "检查叶轮", "subjectStandard": "无磨损",
    "malfunction": "异响", "malfunctionUrl": "", "repairDes": "", "remark": ""}


class FakeMES(KTGClient):
    def __init__(self) -> None:
        self.repairs: dict[int, dict[str, object]] = {8: dict(REPAIR)}
        self.lines: dict[int, dict[str, object]] = {5: dict(LINE)}
        self.mach: dict[str, object] = dict(MACH)
        self.subject: dict[str, object] = dict(SUBJECT)
        self.calls: list[tuple[str, str, Mapping[str, object]]] = []
        self.next_id = 20
        self.fail_write = False
        self.mutate_snapshot_after_post = False
        self.user_id = 7
        self.permissions = sorted({p for group in _PERMS.values() for p in group})

    async def get(self, path: str, params: Mapping[str, object] | None = None) -> dict[str, object]:
        query = params or {}
        self.calls.append(("GET", path, query))
        if path == "/getInfo":
            return {"user": {"userId": self.user_id, "userName": "alice"},
                "permissions": self.permissions}
        if path.endswith("/mes_repair_result"):
            return {"data": [{"dictLabel": "修复", "dictValue": "SUCCESS"}]}
        if path.endswith("/mes_order_status"):
            return {"data": [{"dictLabel": "草稿", "dictValue": "PREPARE"}]}
        if path == "/mes/dv/repair/list":
            rows = [r for r in self.repairs.values() if ("repairCode" not in query or r["repairCode"] == query["repairCode"])]
            return {"rows": rows, "total": len(rows)}
        if path == "/mes/dv/repairline/list":
            rows = [r for r in self.lines.values() if "repairId" not in query or r["repairId"] == query["repairId"]]
            return {"rows": rows, "total": len(rows)}
        if path == "/mes/dv/machinery/list": return {"rows": [self.mach], "total": 1}
        if path == "/mes/dv/dvsubject/list": return {"rows": [self.subject], "total": 1}
        if path == "/mes/dv/machinery/11": return {"data": self.mach}
        if path == "/mes/dv/dvsubject/12": return {"data": self.subject}
        if path.startswith("/mes/dv/repairline/"):
            return {"data": self.lines.get(int(path.rsplit("/", 1)[1]))}
        if path.startswith("/mes/dv/repair/"):
            return {"data": self.repairs.get(int(path.rsplit("/", 1)[1]))}
        raise AssertionError((path, query))

    async def get_current_user(self) -> JsonObject:
        return {"userId": self.user_id, "userName": "alice"}

    async def post(self, path: str, body: Mapping[str, object]) -> dict[str, object]:
        self.calls.append(("POST", path, body))
        if self.fail_write: raise KTGError("KTG_HTTP_ERROR", "timeout")
        row = dict(body)
        if path == "/mes/dv/repair":
            row["repairId"] = self.next_id; self.repairs[self.next_id] = row
            if self.mutate_snapshot_after_post: row["machineryName"] = "MES changed snapshot"
        else:
            row["lineId"] = self.next_id; self.lines[self.next_id] = row
        self.next_id += 1
        return {"code": 200}

    async def put(self, path: str, body: Mapping[str, object]) -> dict[str, object]:
        self.calls.append(("PUT", path, body))
        if self.fail_write: raise KTGError("KTG_HTTP_ERROR", "timeout")
        if path == "/mes/dv/repair": self.repairs[cast(int, body["repairId"])].update(body)
        else: self.lines[cast(int, body["lineId"])].update(body)
        return {"code": 200}

    async def delete(self, path: str) -> dict[str, object]:
        self.calls.append(("DELETE", path, {}))
        if self.fail_write: raise KTGError("KTG_HTTP_ERROR", "timeout")
        rid = int(path.rsplit("/", 1)[1])
        if path.startswith("/mes/dv/repairline/"): self.lines.pop(rid, None)
        else:
            self.repairs.pop(rid, None)
            self.lines = {k: v for k, v in self.lines.items() if v["repairId"] != rid}
        return {"code": 200}


def _ctx(mes: FakeMES, *, trusted: bool = False, operation_id: str | None = None, user_id: int = 7) -> ToolContext:
    return ToolContext(run_id="run", tool_call_id="call", operation_id=operation_id,
        identity={"userId": user_id}, ktg_client=cast(KTGClient, mes),
        channel=ToolChannel.TRUSTED_CONFIRMATION if trusted else ToolChannel.MODEL,
        confirmation_id="confirmed" if trusted else None)


def _run(tool: ToolDefinition, context: ToolContext, args: dict[str, object]) -> ToolResult:
    return asyncio.run(cast(Coroutine[Any, Any, ToolResult], tool.execute(context, args)))


def _tools(tmp_path: Path) -> tuple[DvRepairPreviewStore, dict[str, ToolDefinition]]:
    store = DvRepairPreviewStore(path=tmp_path / "repair.sqlite3")
    return store, {t.name: t for t in build_dv_repair_write_tools(store)}


def _confirm(mes: FakeMES, tools: dict[str, ToolDefinition], action: str,
             args: dict[str, object]) -> tuple[ToolResult, ToolResult]:
    preview = _run(tools[PREVIEW_NAMES[action]], _ctx(mes), args)
    assert preview.status is ToolStatus.SUCCEEDED, preview.content
    result = _run(tools[APPLY_NAMES[action]], _ctx(mes, trusted=True, operation_id=preview.operation_id),
        {"operation_id": preview.operation_id})
    return preview, result


def test_read_scope_ambiguity_options_and_candidates() -> None:
    mes = FakeMES()
    listing = asyncio.run(read_repair_list(mes, {"repair_code": " R-8 ", "page_size": 5}))
    assert listing["total"] == 1 and mes.calls[-1][2]["repairCode"] == "R-8"
    assert cast(list[dict[str, Any]], listing["rows"])[0]["repairId"] == 8
    detail = asyncio.run(read_repair_detail(mes, {"repair_code": "R-8"}))
    assert cast(dict[str, Any], detail["repair"])["repairId"] == 8
    with pytest.raises(KTGError, match="只能提供"):
        asyncio.run(read_repair_detail(mes, {"repair_id": 8, "repair_code": "R-8"}))
    mes.repairs[9] = {**REPAIR, "repairId": 9}
    with pytest.raises(KTGError) as caught:
        asyncio.run(read_repair_detail(mes, {"repair_code": "R-8"}))
    assert caught.value.code == "AMBIGUOUS_MATCH"
    lines = asyncio.run(read_repair_lines(mes, {"repair_id": 8}))
    assert lines["total"] == 1 and cast(list[dict[str, Any]], lines["rows"])[0]["lineId"] == 5
    with pytest.raises(KTGError): asyncio.run(read_repair_lines(mes, {}))
    options = asyncio.run(read_repair_options(mes))
    assert cast(list[dict[str, Any]], options["repair_results"])[0]["value"] == "SUCCESS"
    asyncio.run(read_candidates(mes, {}, "subject"))
    assert "subjectType" not in mes.calls[-1][2] and "enableFlag" not in mes.calls[-1][2]


def test_read_rejects_other_parent_lines() -> None:
    mes = FakeMES()
    mes.lines[5]["repairId"] = 9
    async def wrong_list(path: str, params: Mapping[str, object] | None = None) -> dict[str, object]:
        if path == "/mes/dv/repairline/list": return {"rows": list(mes.lines.values()), "total": 1}
        return await FakeMES.get(mes, path, params)
    mes.get = wrong_list  # type: ignore[method-assign]
    with pytest.raises(KTGError, match="其他维修单"):
        asyncio.run(read_repair_lines(mes, {"repair_id": 8}))


def test_all_six_actions_and_wire_field_boundaries(tmp_path: Path) -> None:
    mes = FakeMES(); _store, tools = _tools(tmp_path)
    _, created = _confirm(mes, tools, "create", {"repair_code": "R-20", "repair_name": "新单",
        "machinery_id": 11, "require_date": "2026-10-25"})
    assert cast(dict[str, Any], created.details)["verified"] is True
    post = next(c for c in mes.calls if c[0] == "POST" and c[1] == "/mes/dv/repair")
    assert set(post[2]) <= {"repairCode", "repairName", "machineryId", "machineryCode", "machineryName",
        "machineryBrand", "machinerySpec", "machineryTypeId", "requireDate", "remark", "status"}
    assert post[2]["status"] == "PREPARE"
    _, changed = _confirm(mes, tools, "update", {"repair_id": 8, "remark": ""})
    assert changed.status is ToolStatus.SUCCEEDED and mes.repairs[8]["remark"] == ""
    assert not any(k in cast(dict[str, object], next(c for c in reversed(mes.calls) if c[0] == "PUT")[2]) for k in
                   ("finishDate", "confirmDate", "acceptedBy", "confirmBy", "createBy"))
    _, line_created = _confirm(mes, tools, "line_create", {"repair_id": 8, "subject_id": 12,
        "malfunction": "振动", "malfunction_url": "", "repair_des": "检查"})
    assert line_created.status is ToolStatus.SUCCEEDED
    new_line_id = cast(int, cast(dict[str, Any], line_created.details)["line_id"])
    _, line_changed = _confirm(mes, tools, "line_update", {"repair_id": 8, "line_id": new_line_id,
        "repair_des": "", "malfunction_url": "photo"})
    assert line_changed.status is ToolStatus.SUCCEEDED and mes.lines[new_line_id]["repairDes"] == ""
    _, line_deleted = _confirm(mes, tools, "line_delete", {"repair_id": 8, "line_id": new_line_id})
    assert line_deleted.status is ToolStatus.SUCCEEDED and new_line_id not in mes.lines
    preview, deleted = _confirm(mes, tools, "delete", {"repair_id": 8})
    assert cast(dict[str, Any], preview.details)["affected_counts"] == {"line_count": 1}
    assert deleted.status is ToolStatus.SUCCEEDED and 8 not in mes.repairs and 5 not in mes.lines


def test_rejects_overreach_non_draft_foreign_line_and_bad_schema(tmp_path: Path) -> None:
    mes = FakeMES(); _store, tools = _tools(tmp_path)
    for action, args in (("create", {"repair_code": "R-9", "repair_name": "x", "machinery_id": 11,
                            "require_date": "2026-10-25", "status": "FINISHED"}),
                         ("update", {"repair_id": 8, "finish_date": "2026-10-25"}),
                         ("line_create", {"repair_id": 8, "subject_id": 12})):
        result = _run(tools[PREVIEW_NAMES[action]], _ctx(mes), cast(dict[str, object], args))
        assert result.status is ToolStatus.FAILED and result.error_code == "VALIDATION_ERROR"
    validator = jsonschema.Draft202012Validator(tools[PREVIEW_NAMES["line_create"]].input_schema)
    with pytest.raises(jsonschema.ValidationError): validator.validate({"repair_id": 8, "subject_id": 12})
    mes.repairs[8]["status"] = "FINISHED"
    result = _run(tools[PREVIEW_NAMES["delete"]], _ctx(mes), {"repair_id": 8})
    assert result.error_code == "REPAIR_NOT_DRAFT"
    mes.repairs[8]["status"] = "PREPARE"
    result = _run(tools[PREVIEW_NAMES["line_update"]], _ctx(mes),
        {"repair_id": 9, "line_id": 5, "repair_des": "bad"})
    assert result.status is ToolStatus.FAILED


def test_trusted_gate_stale_unknown_and_replay(tmp_path: Path) -> None:
    mes = FakeMES(); store, tools = _tools(tmp_path)
    preview = _run(tools[PREVIEW_NAMES["update"]], _ctx(mes), {"repair_id": 8, "repair_name": "新名称"})
    op = cast(str, preview.operation_id)
    assert _run(tools[APPLY_NAMES["update"]], _ctx(mes), {"operation_id": op}).error_code == "CONFIRMATION_REQUIRED"
    assert _run(tools[APPLY_NAMES["update"]], _ctx(mes, trusted=True, operation_id=op, user_id=99),
        {"operation_id": op}).status is ToolStatus.FAILED
    mes.repairs[8]["remark"] = "并发变化"
    stale = _run(tools[APPLY_NAMES["update"]], _ctx(mes, trusted=True, operation_id=op), {"operation_id": op})
    assert stale.error_code == "PREVIEW_STALE" and not any(c[0] == "PUT" for c in mes.calls)
    mes.repairs[8]["remark"] = "旧备注"
    new = _run(tools[PREVIEW_NAMES["create"]], _ctx(mes), {"repair_code": "R-99", "repair_name": "新单",
        "machinery_id": 11, "require_date": "2026-10-25"})
    mes.fail_write = True
    unknown = _run(tools[APPLY_NAMES["create"]], _ctx(mes, trusted=True, operation_id=new.operation_id),
        {"operation_id": new.operation_id})
    assert unknown.status is ToolStatus.UNKNOWN
    replay = _run(tools[APPLY_NAMES["create"]], _ctx(mes, trusted=True, operation_id=new.operation_id),
        {"operation_id": new.operation_id})
    assert replay.details == unknown.details
    assert len([c for c in mes.calls if c[0] == "POST" and c[1] == "/mes/dv/repair"]) == 1
    restarted = DvRepairPreviewStore(path=tmp_path / "repair.sqlite3")
    assert restarted.action_for(cast(str, new.operation_id), "id:7") == "create"


def test_create_snapshot_mismatch_is_unknown_and_not_reposted(tmp_path: Path) -> None:
    mes = FakeMES(); _store, tools = _tools(tmp_path)
    preview = _run(tools[PREVIEW_NAMES["create"]], _ctx(mes), {"repair_code": "R-50",
        "repair_name": "新单", "machinery_id": 11, "require_date": "2026-10-25"})
    assert preview.status is ToolStatus.SUCCEEDED
    mes.mutate_snapshot_after_post = True
    first = _run(tools[APPLY_NAMES["create"]], _ctx(mes, trusted=True, operation_id=preview.operation_id),
        {"operation_id": preview.operation_id})
    assert first.status is ToolStatus.UNKNOWN and cast(dict[str, Any], first.details)["verified"] is False
    replay = _run(tools[APPLY_NAMES["create"]], _ctx(mes, trusted=True, operation_id=preview.operation_id),
        {"operation_id": preview.operation_id})
    assert replay.details == first.details
    assert len([c for c in mes.calls if c[0] == "POST" and c[1] == "/mes/dv/repair"]) == 1


def test_store_ttl_digest_recovery_and_hidden_manifests(tmp_path: Path) -> None:
    store = DvRepairPreviewStore(path=tmp_path / "state.sqlite3", ttl_seconds=0.02)
    op = store.create("id:7", "delete", {"repairId": 8}, None)
    with sqlite3.connect(tmp_path / "state.sqlite3") as db:
        db.execute("UPDATE dv_repair_previews SET before_json=? WHERE preview_id=?", (json.dumps({"repairId": 9}), op))
    with pytest.raises(PreviewError): store.begin(op, "id:7", "delete")
    expired = store.create("id:7", "delete", {"repairId": 8}, None)
    sleep(0.03)
    with pytest.raises(PreviewError, match="过期"): store.begin(expired, "id:7", "delete")
    interrupted = store.create("id:7", "delete", {"repairId": 8}, None)
    assert store.begin(interrupted, "id:7", "delete")[1] is None
    recovered = DvRepairPreviewStore(path=tmp_path / "state.sqlite3", ttl_seconds=0.02)
    _record, receipt = recovered.begin(interrupted, "id:7", "delete")
    assert receipt is not None and receipt.status is ToolStatus.UNKNOWN
    definitions = build_dv_repair_write_tools(store)
    manifests = dv_repair_write_manifests(definitions)
    assert len(build_dv_repair_tools()) == 6 and len(definitions) == 12
    assert all(m.expose_to_model is False for m in manifests if m.name in APPLY_NAMES.values())
    assert all(m.expose_to_model is True for m in manifests if m.name in PREVIEW_NAMES.values())
    assert any(m.name == PREVIEW_NAMES["line_create"] and "mes:dv:repairline:add" in
               next(d.required_permissions for d in definitions if d.name == m.name) for m in manifests)


def test_registration_discovery_and_confirmation_rechecks_line_permissions(tmp_path: Path) -> None:
    mes = FakeMES()
    settings = Settings(model_api_key="fixture", model_base_url="https://example.test/v1", model_id="fixture",
        ktg_base_url="http://localhost", session_db_path=str(tmp_path / "session.sqlite3"),
        workorder_db_path=str(tmp_path / "workorder.sqlite3"),
        dv_repair_db_path=str(tmp_path / "repair.sqlite3"), enable_dv_repair_write=True)
    service = create_service(settings)
    service._ktg_client_factory = lambda _authorization: cast(KTGClient, mes)
    permissions = frozenset(mes.permissions)
    candidate = service._catalog.candidate_snapshot(CatalogIdentity(permissions, "run"), service._switches_provider())
    discovery, _registry, _runner = service._run_discovery(candidate, cast(KTGClient, mes), {"userId": 7})
    discovery.begin_request("request")
    found = asyncio.run(discovery.execute(ToolCall("search", "search_tools", {"query": PREVIEW_NAMES["line_create"], "limit": 20})))
    assert found.details is not None, found.content
    items = cast(list[dict[str, Any]], cast(dict[str, Any], found.details)["items"])
    assert any(item["name"] == PREVIEW_NAMES["line_create"] for item in items)
    assert not any(item["name"] in APPLY_NAMES.values() for item in items)

    preview = _run(service._registry.get(PREVIEW_NAMES["line_create"]), _ctx(mes),
        {"repair_id": 8, "subject_id": 12, "malfunction": "振动"})
    assert preview.status is ToolStatus.SUCCEEDED
    mes.permissions = ["mes:dv:repair:add", "mes:dv:repair:edit", "mes:dv:repair:remove",
        "mes:dv:repair:query", "mes:dv:repair:list", "mes:dv:dvsubject:query"]
    denied = asyncio.run(service.confirm_dv_repair(preview_id=cast(str, preview.operation_id), authorization="Bearer fixture"))
    assert cast(dict[str, Any], denied["result"])["outcome"] == "failed"
    assert not any(call[0] == "POST" and call[1] == "/mes/dv/repairline" for call in mes.calls)
    mes.permissions = sorted({p for group in _PERMS.values() for p in group})
    confirmed = asyncio.run(service.confirm_dv_repair(preview_id=cast(str, preview.operation_id), authorization="Bearer fixture"))
    replayed = asyncio.run(service.confirm_dv_repair(preview_id=cast(str, preview.operation_id), authorization="Bearer fixture"))
    assert cast(dict[str, Any], confirmed["result"])["outcome"] == "succeeded"
    assert replayed == confirmed
    assert len([call for call in mes.calls if call[0] == "POST" and call[1] == "/mes/dv/repairline"]) == 1
