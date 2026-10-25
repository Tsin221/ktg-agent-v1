"""Fake-client tests for guarded T05 preview and confirmation writes."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from time import sleep
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from collections.abc import Coroutine

import jsonschema
import pytest
from http.client import HTTPConnection
from threading import Thread

from ktg_agent.core.messages import JsonObject, ToolCall
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.ktg.dv_checkplan_writes import (APPLY_NAMES, PREVIEW_NAMES, DvCheckPlanPreviewStore,
    _PERMS, build_dv_checkplan_write_tools, dv_checkplan_write_manifests)
from ktg_agent.tools.registry import ToolChannel, ToolContext, ToolDefinition, ToolResult, ToolStatus
from ktg_agent.server import AgentHTTPServer, Settings, create_service
from ktg_agent.sessions import SessionRepository
from ktg_agent.sessions.service import SessionService
from ktg_agent.tools.catalog import CatalogIdentity
from ktg_agent.ktg.workorders import PreviewError


PLAN = {"planId": 8, "planCode": "P-8", "planName": "每周点检", "planType": "CHECK",
        "startDate": "2026-02-01", "endDate": "2026-12-01", "cycleType": "WEEK",
        "cycleCount": 1, "status": "PREPARE", "remark": "note"}
MACH = {"machineryId": 11, "machineryCode": "EQ-11", "machineryName": "泵", "machineryBrand": "A",
        "machinerySpec": "S", "machineryTypeCode": "PUMP", "machineryTypeName": "泵", "status": "STOP"}
SUBJ = {"subjectId": 12, "subjectCode": "S-12", "subjectName": "看压力", "subjectType": "CHECK",
        "subjectContent": "压力", "subjectStandard": "正常", "enableFlag": "Y", "remark": ""}


class FakeMES(KTGClient):
    def __init__(self) -> None:
        self.plans = {8: dict(PLAN)}
        self.subject = dict(SUBJ)
        self.mach_links: list[dict[str, object]] = []
        self.sub_links: list[dict[str, object]] = []
        self.calls: list[tuple[str, str, Mapping[str, object]]] = []
        self.next_id = 20
        self.fail_write = False
        self.fail_verification = False
        self.mutate_subject_on_post = False
        self.keep_links_on_plan_delete = False
        self.plan_list_total_override: int | None = None
        self.user_id = 7
        self.permissions = sorted({p for values in _PERMS.values() for p in values})

    async def get(self, path: str, params: Mapping[str, object] | None = None) -> dict[str, object]:
        params = params or {}; self.calls.append(("GET", path, params))
        if path == "/getInfo":
            return {"user": {"userId": self.user_id, "userName": "alice"}, "permissions": self.permissions}
        if path == "/system/dict/data/type/dv_plan_type": return {"data": [{"dictLabel": "点检", "dictValue": "CHECK"}]}
        if path == "/system/dict/data/type/mes_cycle_type": return {"data": [{"dictLabel": "周", "dictValue": "WEEK"}]}
        if path == "/system/dict/data/type/mes_order_status": return {"data": [{"dictLabel": "草稿", "dictValue": "PREPARE"}, {"dictLabel": "启用", "dictValue": "FINISHED"}]}
        if path == "/mes/dv/checkplan/list":
            if self.fail_verification and any(call[0] == "POST" and call[1] == "/mes/dv/checkplan" for call in self.calls):
                raise KTGError("PERMISSION_DENIED", "permission changed", status=403)
            code = params.get("planCode")
            rows = [p for p in self.plans.values() if code is None or p["planCode"] == code]
            return {"rows": rows, "total": self.plan_list_total_override if self.plan_list_total_override is not None else len(rows)}
        if path.startswith("/mes/dv/checkplan/"):
            pid = int(path.rsplit("/", 1)[1]); return {"data": self.plans.get(pid)}
        if path == "/mes/dv/machinery/11": return {"data": MACH}
        if path == "/mes/dv/dvsubject/12": return {"data": self.subject}
        if path.startswith("/mes/dv/checkmachinery/") and path != "/mes/dv/checkmachinery/list":
            rid = int(path.rsplit("/", 1)[1]); return {"data": next((r for r in self.mach_links if r["recordId"] == rid), None)}
        if path.startswith("/mes/dv/checksubject/") and path != "/mes/dv/checksubject/list":
            rid = int(path.rsplit("/", 1)[1]); return {"data": next((r for r in self.sub_links if r["recordId"] == rid), None)}
        if path == "/mes/dv/checkmachinery/list":
            rows = self.mach_links
            if "planId" in params: rows = [r for r in rows if r["planId"] == params["planId"]]
            if "machineryId" in params: rows = [r for r in rows if r["machineryId"] == params["machineryId"]]
            return {"rows": rows, "total": len(rows)}
        if path == "/mes/dv/checksubject/list":
            rows = [r for r in self.sub_links if "planId" not in params or r["planId"] == params["planId"]]
            return {"rows": rows, "total": len(rows)}
        raise AssertionError((path, params))

    async def get_current_user(self) -> dict[str, Any]:
        return {"userId": self.user_id, "userName": "alice"}

    async def post(self, path: str, body: Mapping[str, object]) -> dict[str, object]:
        self.calls.append(("POST", path, body))
        if self.fail_write: raise KTGError("KTG_HTTP_ERROR", "transport uncertain")
        if path == "/mes/dv/checkplan":
            new = dict(body); new["planId"] = self.next_id; self.next_id += 1; self.plans[cast(int, new["planId"])] = new
        elif path == "/mes/dv/checkmachinery":
            row = {"recordId": self.next_id, **body}; self.next_id += 1
            self.mach_links.append(row)
        elif path == "/mes/dv/checksubject":
            # Server owns the snapshot and saves current source fields.
            if self.mutate_subject_on_post:
                self.subject["subjectContent"] = "确认前源数据变化"
            row = {"recordId": self.next_id, "planId": body["planId"], "subjectId": body["subjectId"],
                   "subjectCode": self.subject["subjectCode"], "subjectName": self.subject["subjectName"], "subjectType": self.subject["subjectType"],
                   "subjectContent": self.subject["subjectContent"], "subjectStandard": self.subject["subjectStandard"]}; self.next_id += 1
            self.sub_links.append(row)
        return {"code": 200}

    async def put(self, path: str, body: Mapping[str, object]) -> dict[str, object]:
        self.calls.append(("PUT", path, body))
        if self.fail_write: raise KTGError("KTG_HTTP_ERROR", "transport uncertain")
        self.plans[cast(int, body["planId"])].update(body)
        return {"code": 200}

    async def delete(self, path: str) -> dict[str, object]:
        self.calls.append(("DELETE", path, {}))
        if self.fail_write: raise KTGError("KTG_HTTP_ERROR", "transport uncertain")
        if path.startswith("/mes/dv/checkplan/"):
            pid = int(path.rsplit("/", 1)[1]); self.plans.pop(pid, None)
            if not self.keep_links_on_plan_delete:
                self.mach_links = [r for r in self.mach_links if r["planId"] != pid]
                self.sub_links = [r for r in self.sub_links if r["planId"] != pid]
        elif path.startswith("/mes/dv/checkmachinery/"):
            rid = int(path.rsplit("/", 1)[1]); self.mach_links = [r for r in self.mach_links if r["recordId"] != rid]
        else:
            rid = int(path.rsplit("/", 1)[1]); self.sub_links = [r for r in self.sub_links if r["recordId"] != rid]
        return {"code": 200}


def _ctx(client: FakeMES, *, trusted: bool, operation_id: str | None = None) -> ToolContext:
    return ToolContext(run_id="run", tool_call_id="call", operation_id=operation_id,
        identity={"userId": 7}, ktg_client=cast(KTGClient, client),
        channel=ToolChannel.TRUSTED_CONFIRMATION if trusted else ToolChannel.MODEL,
        confirmation_id="confirmed" if trusted else None)


def _tools(tmp_path: Path) -> tuple[DvCheckPlanPreviewStore, dict[str, ToolDefinition]]:
    store = DvCheckPlanPreviewStore(path=tmp_path / "checkplan.sqlite3")
    return store, {t.name: t for t in build_dv_checkplan_write_tools(store)}


def _run(tool: ToolDefinition, context: ToolContext, args: dict[str, object]) -> ToolResult:
    return asyncio.run(cast(Coroutine[Any, Any, ToolResult], tool.execute(context, args)))


def _preview_and_confirm(client: FakeMES, tools: dict[str, ToolDefinition], action: str,
                         args: dict[str, object]) -> tuple[ToolResult, ToolResult]:
    preview = _run(tools[PREVIEW_NAMES[action]], _ctx(client, trusted=False), args)
    assert preview.status is ToolStatus.SUCCEEDED, preview.content
    result = _run(tools[APPLY_NAMES[action]], _ctx(client, trusted=True, operation_id=preview.operation_id),
                  {"operation_id": preview.operation_id})
    assert cast(dict[str, Any], result.details)["kind"] == "dv_checkplan_result"
    return preview, result


def test_all_seven_actions_preview_and_verified_confirmation(tmp_path: Path) -> None:
    client = FakeMES(); store, tools = _tools(tmp_path)
    _, created = _preview_and_confirm(client, tools, "create", {"plan_code": "P-9", "plan_name": "新计划",
        "plan_type": "CHECK", "cycle_type": "WEEK", "cycle_count": 2})
    assert cast(dict[str, Any], created.details)["outcome"] == "succeeded" and cast(dict[str, Any], created.details)["verified"] is True
    _, edited = _preview_and_confirm(client, tools, "update", {"plan_id": 8, "transition": "edit", "remark": "new"})
    assert cast(dict[str, Any], edited.details)["outcome"] == "succeeded", edited.details
    assert cast(dict[str, Any], edited.details)["plan"]["remark"] == "new"
    client.mach_links.append({"recordId": 30, "planId": 8, "machineryId": 99, "machineryCode": "OLD",
        "machineryName": "old", "machineryBrand": "B", "machinerySpec": "x"})
    client.sub_links.append({"recordId": 31, "planId": 8, "subjectId": 98, "subjectCode": "S-OLD", "subjectName": "old",
        "subjectType": "CHECK", "subjectContent": "old", "subjectStandard": "old"})
    _, added_m = _preview_and_confirm(client, tools, "machinery_add", {"plan_id": 8, "machinery_id": 11})
    assert cast(dict[str, Any], added_m.details)["record_id"]
    _, removed_m = _preview_and_confirm(client, tools, "machinery_remove", {"plan_id": 8, "record_id": 30})
    assert cast(dict[str, Any], removed_m.details)["outcome"] == "succeeded"
    _, added_s = _preview_and_confirm(client, tools, "subject_add", {"plan_id": 8, "subject_id": 12})
    post = next(call for call in client.calls if call[0] == "POST" and call[1] == "/mes/dv/checksubject")
    assert dict(post[2]) == {"planId": 8, "subjectId": 12}
    _, removed_s = _preview_and_confirm(client, tools, "subject_remove", {"plan_id": 8, "record_id": 31})
    assert cast(dict[str, Any], removed_s.details)["outcome"] == "succeeded"
    _, deleted = _preview_and_confirm(client, tools, "delete", {"plan_id": 8})
    assert cast(dict[str, Any], deleted.details)["verified"] is True
    assert len([c for c in client.calls if c[0] == "POST"]) == 3


def test_update_transitions_are_mutually_exclusive_and_guarded(tmp_path: Path) -> None:
    client = FakeMES(); store, tools = _tools(tmp_path)
    schema = tools[PREVIEW_NAMES["update"]].input_schema
    jsonschema.Draft202012Validator(schema).validate({"plan_id": 8, "transition": "activate"})
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate({"plan_id": 8, "transition": "activate", "plan_name": "x"})
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate({"plan_id": 8, "transition": "edit"})
    result = _run(tools[PREVIEW_NAMES["update"]], _ctx(client, trusted=False),
                  {"plan_id": 8, "transition": "activate"})
    assert result.status is ToolStatus.FAILED and result.error_code == "PLAN_LINKS_REQUIRED"

    client.mach_links.append({"recordId": 30, "planId": 8, "machineryId": 99, "machineryCode": "OLD",
        "machineryName": "old", "machineryBrand": "B", "machinerySpec": "x"})
    client.sub_links.append({"recordId": 31, "planId": 8, "subjectId": 98, "subjectCode": "S-OLD", "subjectName": "old",
        "subjectType": "CHECK", "subjectContent": "old", "subjectStandard": "old"})
    preview = _run(tools[PREVIEW_NAMES["update"]], _ctx(client, trusted=False), {"plan_id": 8, "transition": "activate"})
    assert cast(dict[str, Any], preview.details)["affected_counts"] == {"machinery_count": 1, "subject_count": 1}
    client.mach_links[0]["machineryName"] = "changed without count change"
    stale = _run(tools[APPLY_NAMES["update"]], _ctx(client, trusted=True, operation_id=preview.operation_id),
        {"operation_id": preview.operation_id})
    assert cast(dict[str, Any], stale.details)["outcome"] == "failed" and cast(dict[str, Any], stale.details)["error_code"] == "PREVIEW_STALE"
    assert not any(call[0] == "PUT" for call in client.calls)

    client = FakeMES(); _, tools = _tools(tmp_path / "activate")
    client.mach_links.append({"recordId": 30, "planId": 8, "machineryId": 99, "machineryCode": "OLD",
        "machineryName": "old", "machineryBrand": "B", "machinerySpec": "x"})
    client.sub_links.append({"recordId": 31, "planId": 8, "subjectId": 98, "subjectCode": "S-OLD", "subjectName": "old",
        "subjectType": "CHECK", "subjectContent": "old", "subjectStandard": "old"})
    _, activated = _preview_and_confirm(client, tools, "update", {"plan_id": 8, "transition": "activate"})
    assert cast(dict[str, Any], activated.details)["outcome"] == "succeeded" and client.plans[8]["status"] == "FINISHED"
    _, deactivated = _preview_and_confirm(client, tools, "update", {"plan_id": 8, "transition": "deactivate"})
    assert cast(dict[str, Any], deactivated.details)["outcome"] == "succeeded" and client.plans[8]["status"] == "PREPARE"


def test_one_shot_unknown_never_reposts_and_model_cannot_apply(tmp_path: Path) -> None:
    client = FakeMES(); store, tools = _tools(tmp_path)
    preview = _run(tools[PREVIEW_NAMES["create"]], _ctx(client, trusted=False), {"plan_code": "P-9", "plan_name": "新计划",
        "plan_type": "CHECK", "cycle_type": "WEEK", "cycle_count": 2})
    denied = _run(tools[APPLY_NAMES["create"]], _ctx(client, trusted=False), {"operation_id": preview.operation_id})
    assert denied.error_code == "CONFIRMATION_REQUIRED"
    client.fail_write = True
    unknown = _run(tools[APPLY_NAMES["create"]], _ctx(client, trusted=True, operation_id=preview.operation_id), {"operation_id": preview.operation_id})
    again = _run(tools[APPLY_NAMES["create"]], _ctx(client, trusted=True, operation_id=preview.operation_id), {"operation_id": preview.operation_id})
    assert unknown.status is ToolStatus.UNKNOWN and unknown.details == again.details
    assert len([c for c in client.calls if c[0] == "POST" and c[1] == "/mes/dv/checkplan"]) == 1


def test_manifest_and_schema_permissions_and_visibility(tmp_path: Path) -> None:
    _, tools = _tools(tmp_path)
    manifests = {m.name: m for m in dv_checkplan_write_manifests(tuple(tools.values()))}
    for action in PREVIEW_NAMES:
        preview, apply = tools[PREVIEW_NAMES[action]], tools[APPLY_NAMES[action]]
        assert preview.expose_to_model and preview.effect.value == "read"
        assert not apply.expose_to_model and apply.effect.value == "write"
        assert manifests[apply.name].confirmation_policy == "trusted_confirmation"
        assert manifests[preview.name].proposal_store_effect is True
        assert manifests[preview.name].permission_policy == manifests[apply.name].permission_policy


def test_create_rejects_invalid_calendar_and_shared_duplicate_number(tmp_path: Path) -> None:
    client = FakeMES(); _, tools = _tools(tmp_path)
    for start, end in (("2026-02-30", "2026-03-01"), ("2026-03-02", "2026-03-01")):
        result = _run(tools[PREVIEW_NAMES["create"]], _ctx(client, trusted=False), {"plan_code": "P-9",
            "plan_name": "新计划", "plan_type": "CHECK", "cycle_type": "WEEK", "cycle_count": 1,
            "start_date": start, "end_date": end})
        assert result.status is ToolStatus.FAILED
    result = _run(tools[PREVIEW_NAMES["create"]], _ctx(client, trusted=False), {"plan_code": "P-8",
        "plan_name": "重复编号", "plan_type": "MAINTEN", "cycle_type": "WEEK", "cycle_count": 1})
    assert result.error_code == "PLAN_CODE_EXISTS"


def test_write_accepted_then_verification_permission_error_is_cached_unknown(tmp_path: Path) -> None:
    client = FakeMES(); _, tools = _tools(tmp_path)
    preview = _run(tools[PREVIEW_NAMES["create"]], _ctx(client, trusted=False), {"plan_code": "P-9",
        "plan_name": "新计划", "plan_type": "CHECK", "cycle_type": "WEEK", "cycle_count": 2})
    client.fail_verification = True
    ctx = _ctx(client, trusted=True, operation_id=preview.operation_id)
    result = _run(tools[APPLY_NAMES["create"]], ctx, {"operation_id": preview.operation_id})
    replay = _run(tools[APPLY_NAMES["create"]], ctx, {"operation_id": preview.operation_id})
    assert result.status is ToolStatus.UNKNOWN and cast(dict[str, Any], result.details)["verified"] is False
    assert replay.details == result.details
    assert "P-9" in [plan["planCode"] for plan in client.plans.values()]
    assert len([call for call in client.calls if call[0] == "POST" and call[1] == "/mes/dv/checkplan"]) == 1


def test_association_snapshot_mismatch_and_delete_orphan_links_are_not_success(tmp_path: Path) -> None:
    client = FakeMES(); _, tools = _tools(tmp_path)
    preview = _run(tools[PREVIEW_NAMES["subject_add"]], _ctx(client, trusted=False), {"plan_id": 8, "subject_id": 12})
    client.mutate_subject_on_post = True
    result = _run(tools[APPLY_NAMES["subject_add"]], _ctx(client, trusted=True, operation_id=preview.operation_id),
        {"operation_id": preview.operation_id})
    assert result.status is ToolStatus.UNKNOWN and cast(dict[str, Any], result.details)["verified"] is False
    assert client.sub_links[0]["subjectContent"] != cast(dict[str, Any], cast(dict[str, Any], preview.details)["after"])["subjectContent"]

    client = FakeMES(); _, tools = _tools(tmp_path / "orphan")
    client.mach_links.append({"recordId": 30, "planId": 8, "machineryId": 99, "machineryCode": "OLD",
        "machineryName": "old", "machineryBrand": "B", "machinerySpec": "x"})
    preview = _run(tools[PREVIEW_NAMES["delete"]], _ctx(client, trusted=False), {"plan_id": 8})
    client.keep_links_on_plan_delete = True
    result = _run(tools[APPLY_NAMES["delete"]], _ctx(client, trusted=True, operation_id=preview.operation_id),
        {"operation_id": preview.operation_id})
    assert result.status is ToolStatus.UNKNOWN and cast(dict[str, Any], result.details)["verified"] is False


def test_incomplete_plan_code_scan_fails_closed_and_read_schemas_require_selectors(tmp_path: Path) -> None:
    client = FakeMES(); client.plan_list_total_override = 2
    _, tools = _tools(tmp_path)
    result = _run(tools[PREVIEW_NAMES["create"]], _ctx(client, trusted=False), {"plan_code": "P-9",
        "plan_name": "新计划", "plan_type": "CHECK", "cycle_type": "WEEK", "cycle_count": 1})
    assert result.error_code == "KTG_INVALID_RESPONSE"
    from ktg_agent.ktg.dv_checkplan import build_dv_checkplan_tools
    definitions = {tool.name: tool for tool in build_dv_checkplan_tools()}
    detail = jsonschema.Draft202012Validator(definitions["ktg_dv_checkplan_detail"].input_schema)
    assert not list(detail.iter_errors({"plan_id": 8}))
    assert list(detail.iter_errors({})) and list(detail.iter_errors({"plan_id": 8, "plan_code": "P-8"}))
    links = jsonschema.Draft202012Validator(definitions["ktg_dv_checkplan_links"].input_schema)
    assert not list(links.iter_errors({"plan_id": 8, "kind": "subject"}))
    assert list(links.iter_errors({"plan_id": 8}))
    candidates = jsonschema.Draft202012Validator(definitions["ktg_dv_checkplan_subject_candidates"].input_schema)
    assert list(candidates.iter_errors({}))


def test_preview_store_identity_ttl_digest_and_interrupted_execution_recovery(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    store = DvCheckPlanPreviewStore(path=path, ttl_seconds=0.02)
    preview_id = store.create("id:7", "delete", {"planId": 8}, None)
    with pytest.raises(PreviewError, match="不属于"):
        store.action_for(preview_id, "id:8")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE dv_checkplan_previews SET after_json=? WHERE preview_id=?", ('{"planId":99}', preview_id))
    with pytest.raises(PreviewError, match="校验失败"):
        store.begin(preview_id, "id:7", "delete")

    expired = store.create("id:7", "delete", {"planId": 8}, None)
    sleep(0.03)
    with pytest.raises(PreviewError, match="过期"):
        store.begin(expired, "id:7", "delete")

    interrupted = store.create("id:7", "delete", {"planId": 8}, None)
    assert store.begin(interrupted, "id:7", "delete")[1] is None
    restarted = DvCheckPlanPreviewStore(path=path, ttl_seconds=0.02)
    recovered, receipt = restarted.begin(interrupted, "id:7", "delete")
    assert recovered["state"] == "completed" and receipt is not None
    assert receipt.status is ToolStatus.UNKNOWN and "勿重复提交" in receipt.content


def test_queryservice_discovery_hides_apply_and_http_confirmation_replays_receipt(tmp_path: Path) -> None:
    mes = FakeMES()
    settings = Settings(model_api_key="test", model_base_url="https://example.test/v1", model_id="test",
        ktg_base_url="http://localhost", session_db_path=str(tmp_path / "session.sqlite3"),
        workorder_db_path=str(tmp_path / "workorder.sqlite3"),
        dv_checkplan_db_path=str(tmp_path / "checkplan.sqlite3"), enable_dv_checkplan_write=True)
    service = create_service(settings)
    service._ktg_client_factory = lambda _authorization: cast(KTGClient, mes)
    permissions = frozenset(p for group in _PERMS.values() for p in group)
    candidate = service._catalog.candidate_snapshot(CatalogIdentity(permissions, "run"), service._switches_provider())
    discovery, registry, _runner = service._run_discovery(candidate, cast(KTGClient, mes), {"userId": 7})
    discovery.begin_request("r1")
    found = asyncio.run(discovery.execute(ToolCall("s", "search_tools", {"query": PREVIEW_NAMES["create"], "limit": 20})))
    assert found.details
    discovery_details = cast(dict[str, Any], found.details)
    matches = [{"name": item["name"], "version": item["version"]}
               for item in discovery_details["items"] if item["name"] == PREVIEW_NAMES["create"]]
    assert len(matches) == 1
    assert not any(item["name"] in APPLY_NAMES.values() for item in discovery_details["items"])
    loaded = asyncio.run(discovery.execute(ToolCall("l", "load_tools", cast(JsonObject, {"tools": matches}))))
    assert loaded.status is ToolStatus.SUCCEEDED
    next_request = discovery.begin_request("r2")
    assert PREVIEW_NAMES["create"] in next_request.names and APPLY_NAMES["create"] not in next_request.names

    assert service._dv_checkplan_store is not None
    preview = _run(service._registry.get(PREVIEW_NAMES["delete"]), _ctx(mes, trusted=False), {"plan_id": 8})
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    session_service = SessionService(repo, lambda _authorization: cast(KTGClient, mes))
    server = AgentHTTPServer(("127.0.0.1", 0), service, session_service)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        def confirm() -> dict[str, object]:
            connection = HTTPConnection("127.0.0.1", server.server_port)
            connection.request("POST", "/agent-api/v1/dv-checkplan/confirm",
            body=json.dumps({"preview_id": preview.operation_id}).encode(),
                headers={"Authorization": "Bearer fixture", "Content-Type": "application/json"})
            response = connection.getresponse(); payload = json.loads(response.read()); connection.close()
            assert response.status == 200
            return cast(dict[str, object], payload["result"])
        mes.user_id = 8
        wrong_user = confirm()
        assert wrong_user["outcome"] == "failed" and wrong_user["error_code"] == "PREVIEW_NOT_FOUND"
        mes.user_id = 7
        mes.permissions = ["mes:dv:checkplan:remove", "mes:dv:checkplan:query"]
        denied = confirm()
        assert denied["outcome"] == "failed" and not any(c[0] == "DELETE" for c in mes.calls)
        mes.permissions = sorted({p for group in _PERMS.values() for p in group})
        first_result, replay = confirm(), confirm()
        assert first_result["outcome"] == "succeeded" and first_result["verified"] is True
        assert replay == first_result
        assert len([call for call in mes.calls if call[0] == "DELETE" and call[1] == "/mes/dv/checkplan/8"]) == 1
    finally:
        server.shutdown(); thread.join(); server.server_close()
