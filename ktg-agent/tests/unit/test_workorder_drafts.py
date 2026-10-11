import asyncio
from collections.abc import Mapping

from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.ktg.workorders import PreviewStore, build_workorder_tools
from ktg_agent.tools import ToolContext, ToolStatus


def test_create_preview_resolves_product_and_confirmation_writes_once() -> None:
    asyncio.run(_create_preview_resolves_product_and_confirmation_writes_once())


async def _create_preview_resolves_product_and_confirmation_writes_once() -> None:
    writes: list[tuple[str, str, Mapping[str, object]]] = []

    async def read_transport(
        url: str,
        _headers: Mapping[str, str],
        params: Mapping[str, object],
        _timeout: float,
    ) -> object:
        if url.endswith("/mes/md/mditem/list"):
            return {
                "code": 200,
                "total": 1,
                "rows": [{
                    "itemId": 11,
                    "itemCode": "A-01",
                    "itemName": "产品 A",
                    "unitOfMeasure": "件",
                    "itemOrProduct": "PRODUCT",
                }],
            }
        if url.endswith("/system/autocode/get/WORKORDER_CODE"):
            return "WO-20261011-001"
        if url.endswith("/mes/pro/workorder/51"):
            return {"code": 200, "data": _saved_workorder()}
        if url.endswith("/mes/pro/workorderbom/list"):
            assert params["workorderId"] == 51
            return {"code": 200, "total": 1, "rows": [{"workorderId": 51}]}
        raise AssertionError(url)

    async def write_transport(
        method: str,
        url: str,
        _headers: Mapping[str, str],
        body: Mapping[str, object],
        _timeout: float,
    ) -> dict[str, object]:
        writes.append((method, url, body))
        return {"code": 200, "data": 51}

    client = KTGClient(
        base_url="http://127.0.0.1:8080",
        authorization="Bearer current-user-token",
        transport=read_transport,
        write_transport=write_transport,
    )
    store = PreviewStore(ttl_seconds=600)
    tools = {tool.name: tool for tool in build_workorder_tools(store)}
    preview = await tools["ktg_preview_create_workorder_draft"].execute(
        _context("preview-call", client, user_id=7),
        {
            "product_name": "产品 A",
            "quantity": 100,
            "request_date": "2026-10-31",
        },
    )

    assert preview.status is ToolStatus.SUCCEEDED
    assert preview.details is not None
    assert preview.details["kind"] == "workorder_draft_preview"
    assert preview.details["product"] == {
        "itemId": 11,
        "itemCode": "A-01",
        "itemName": "产品 A",
        "unitOfMeasure": "件",
    }
    assert preview.details["quantity"] == "100"
    assert preview.details["workorderCode"] == "WO-20261011-001"
    preview_id = preview.details["preview_id"]
    assert isinstance(preview_id, str)

    confirmed = await tools["ktg_create_workorder_draft"].execute(
        _context("confirm-call", client, user_id=7, operation_id=preview_id),
        {"preview_id": preview_id},
    )
    repeated = await tools["ktg_create_workorder_draft"].execute(
        _context("repeat-call", client, user_id=7, operation_id=preview_id),
        {"preview_id": preview_id},
    )

    assert confirmed.status is ToolStatus.SUCCEEDED
    assert confirmed.details is not None
    assert confirmed.details["kind"] == "workorder_draft_result"
    assert confirmed.details["outcome"] == "succeeded"
    assert confirmed.details["workorderId"] == 51
    assert confirmed.details["bom_verified"] is True
    assert repeated.details == confirmed.details
    assert len(writes) == 1
    method, url, body = writes[0]
    assert method == "POST"
    assert url.endswith("/mes/pro/workorder")
    assert body == {
        "workorderCode": "WO-20261011-001",
        "workorderName": "产品 A 工单",
        "workorderType": "SELF",
        "orderSource": "MANUAL",
        "productId": 11,
        "quantity": "100",
        "requestDate": "2026-10-31",
        "parentId": 0,
    }


def test_preview_rejects_ambiguous_product_without_creating_approval() -> None:
    asyncio.run(_preview_rejects_ambiguous_product_without_creating_approval())


async def _preview_rejects_ambiguous_product_without_creating_approval() -> None:
    async def read_transport(
        url: str,
        _headers: Mapping[str, str],
        _params: Mapping[str, object],
        _timeout: float,
    ) -> object:
        assert url.endswith("/mes/md/mditem/list")
        return {
            "code": 200,
            "total": 2,
            "rows": [
                {"itemId": 1, "itemCode": "A-01", "itemName": "产品 A"},
                {"itemId": 2, "itemCode": "A-02", "itemName": "产品 A"},
            ],
        }

    client = KTGClient(
        base_url="http://127.0.0.1:8080",
        authorization="Bearer current-user-token",
        transport=read_transport,
    )
    tools = {
        tool.name: tool for tool in build_workorder_tools(PreviewStore(ttl_seconds=600))
    }
    result = await tools["ktg_preview_create_workorder_draft"].execute(
        _context("preview-call", client, user_id=7),
        {
            "product_name": "产品 A",
            "quantity": 100,
            "request_date": "2026-10-31",
        },
    )

    assert result.status is ToolStatus.FAILED
    assert result.error_code == "AMBIGUOUS_PRODUCT"
    assert result.details is not None
    assert result.details["kind"] == "workorder_product_candidates"
    candidates = result.details["candidates"]
    assert isinstance(candidates, list)
    assert len(candidates) == 2


def test_confirmation_is_bound_to_preview_user() -> None:
    asyncio.run(_confirmation_is_bound_to_preview_user())


async def _confirmation_is_bound_to_preview_user() -> None:
    client = _preview_only_client()
    store = PreviewStore(ttl_seconds=600)
    tools = {tool.name: tool for tool in build_workorder_tools(store)}
    preview = await tools["ktg_preview_create_workorder_draft"].execute(
        _context("preview-call", client, user_id=7),
        {
            "product_code": "A-01",
            "quantity": "3.5",
            "request_date": "2026-10-31",
        },
    )
    assert preview.details is not None
    preview_id = preview.details["preview_id"]
    assert isinstance(preview_id, str)

    result = await tools["ktg_create_workorder_draft"].execute(
        _context("confirm-call", client, user_id=8, operation_id=preview_id),
        {"preview_id": preview_id},
    )

    assert result.status is ToolStatus.FAILED
    assert result.error_code == "APPROVAL_IDENTITY_MISMATCH"


def test_create_timeout_reconciles_by_stable_workorder_code_without_retry() -> None:
    asyncio.run(_create_timeout_reconciles_by_stable_workorder_code_without_retry())


async def _create_timeout_reconciles_by_stable_workorder_code_without_retry() -> None:
    write_attempts = 0

    async def read_transport(
        url: str,
        _headers: Mapping[str, str],
        _params: Mapping[str, object],
        _timeout: float,
    ) -> object:
        if url.endswith("/mes/md/mditem/list"):
            return {"code": 200, "total": 1, "rows": [_product()]}
        if url.endswith("/system/autocode/get/WORKORDER_CODE"):
            return "WO-20261011-001"
        if url.endswith("/mes/pro/workorder/list"):
            return {"code": 200, "total": 1, "rows": [_saved_workorder()]}
        if url.endswith("/mes/pro/workorder/51"):
            return {"code": 200, "data": _saved_workorder()}
        if url.endswith("/mes/pro/workorderbom/list"):
            return {"code": 200, "total": 0, "rows": []}
        raise AssertionError(url)

    async def write_transport(
        _method: str,
        _url: str,
        _headers: Mapping[str, str],
        _body: Mapping[str, object],
        _timeout: float,
    ) -> dict[str, object]:
        nonlocal write_attempts
        write_attempts += 1
        raise KTGError("KTG_CONNECTION_ERROR", "连接中断")

    client = KTGClient(
        base_url="http://127.0.0.1:8080",
        authorization="Bearer current-user-token",
        transport=read_transport,
        write_transport=write_transport,
    )
    store = PreviewStore(ttl_seconds=600)
    tools = {tool.name: tool for tool in build_workorder_tools(store)}
    preview = await tools["ktg_preview_create_workorder_draft"].execute(
        _context("preview-call", client, user_id=7),
        {
            "product_code": "A-01",
            "quantity": 100,
            "request_date": "2026-10-31",
        },
    )
    assert preview.details is not None
    preview_id = preview.details["preview_id"]
    assert isinstance(preview_id, str)

    result = await tools["ktg_create_workorder_draft"].execute(
        _context("confirm-call", client, user_id=7, operation_id=preview_id),
        {"preview_id": preview_id},
    )

    assert result.status is ToolStatus.SUCCEEDED
    assert result.verification is not None
    assert result.verification["reconciled_after_error"] is True
    assert write_attempts == 1


def test_create_timeout_with_failed_reconciliation_stays_unknown_without_retry() -> None:
    asyncio.run(_create_timeout_with_failed_reconciliation_stays_unknown_without_retry())


async def _create_timeout_with_failed_reconciliation_stays_unknown_without_retry() -> None:
    write_attempts = 0

    async def read_transport(
        url: str,
        _headers: Mapping[str, str],
        _params: Mapping[str, object],
        _timeout: float,
    ) -> object:
        if url.endswith("/mes/md/mditem/list"):
            return {"code": 200, "total": 1, "rows": [_product()]}
        if url.endswith("/system/autocode/get/WORKORDER_CODE"):
            return "WO-20261011-001"
        if url.endswith("/mes/pro/workorder/list"):
            raise KTGError("KTG_CONNECTION_ERROR", "回读连接中断")
        raise AssertionError(url)

    async def write_transport(
        _method: str,
        _url: str,
        _headers: Mapping[str, str],
        _body: Mapping[str, object],
        _timeout: float,
    ) -> dict[str, object]:
        nonlocal write_attempts
        write_attempts += 1
        raise KTGError("KTG_CONNECTION_ERROR", "写入响应中断")

    client = KTGClient(
        base_url="http://127.0.0.1:8080",
        authorization="Bearer current-user-token",
        transport=read_transport,
        write_transport=write_transport,
    )
    store = PreviewStore(ttl_seconds=600)
    tools = {tool.name: tool for tool in build_workorder_tools(store)}
    preview = await tools["ktg_preview_create_workorder_draft"].execute(
        _context("preview-call", client, user_id=7),
        {
            "product_code": "A-01",
            "quantity": 100,
            "request_date": "2026-10-31",
        },
    )
    assert preview.details is not None
    preview_id = preview.details["preview_id"]
    assert isinstance(preview_id, str)

    result = await tools["ktg_create_workorder_draft"].execute(
        _context("confirm-call", client, user_id=7, operation_id=preview_id),
        {"preview_id": preview_id},
    )
    repeated = await tools["ktg_create_workorder_draft"].execute(
        _context("repeat-call", client, user_id=7, operation_id=preview_id),
        {"preview_id": preview_id},
    )

    assert result.status is ToolStatus.UNKNOWN
    assert result.error_code == "WRITE_RESULT_UNKNOWN"
    assert result.details is not None
    assert result.details["outcome"] == "unknown"
    assert result.details["label"] == "待核实"
    assert result.verification == {"reconciled_after_error": True}
    assert repeated.details == result.details
    assert write_attempts == 1


def test_edit_preview_merges_allowed_change_and_checks_version_before_write() -> None:
    asyncio.run(_edit_preview_merges_allowed_change_and_checks_version_before_write())


async def _edit_preview_merges_allowed_change_and_checks_version_before_write() -> None:
    detail_reads = 0
    written_body: Mapping[str, object] | None = None

    async def read_transport(
        url: str,
        _headers: Mapping[str, str],
        _params: Mapping[str, object],
        _timeout: float,
    ) -> object:
        nonlocal detail_reads
        if url.endswith("/mes/pro/workorder/list"):
            return {"code": 200, "total": 1, "rows": [_editable_workorder(100, 3)]}
        if url.endswith("/mes/pro/workorder/51"):
            detail_reads += 1
            return {
                "code": 200,
                "data": _editable_workorder(120 if detail_reads >= 3 else 100, 4 if detail_reads >= 3 else 3),
            }
        if url.endswith("/mes/md/mditem/list"):
            return {"code": 200, "total": 1, "rows": [_product()]}
        raise AssertionError(url)

    async def write_transport(
        method: str,
        url: str,
        _headers: Mapping[str, str],
        body: Mapping[str, object],
        _timeout: float,
    ) -> dict[str, object]:
        nonlocal written_body
        assert method == "PUT"
        assert url.endswith("/mes/pro/workorder")
        written_body = body
        return {"code": 200, "data": 1}

    client = KTGClient(
        base_url="http://127.0.0.1:8080",
        authorization="Bearer current-user-token",
        transport=read_transport,
        write_transport=write_transport,
    )
    store = PreviewStore(ttl_seconds=600)
    tools = {tool.name: tool for tool in build_workorder_tools(store)}
    preview = await tools["ktg_preview_edit_workorder_draft"].execute(
        _context("preview-edit", client, user_id=7),
        {"target_workorder_code": "WO-001", "quantity": 120},
    )
    assert preview.status is ToolStatus.SUCCEEDED
    assert preview.details is not None
    assert preview.details["changes"] == [
        {"field": "quantity", "before": "100", "after": "120"}
    ]
    preview_id = preview.details["preview_id"]
    assert isinstance(preview_id, str)

    result = await tools["ktg_edit_workorder_draft"].execute(
        _context("confirm-edit", client, user_id=7, operation_id=preview_id),
        {"preview_id": preview_id},
    )

    assert result.status is ToolStatus.SUCCEEDED
    assert written_body is not None
    assert written_body["workorderId"] == 51
    assert written_body["version"] == 3
    assert written_body["quantity"] == "120"
    assert "status" not in written_body


def _editable_workorder(quantity: int, version: int) -> dict[str, object]:
    return {
        **_saved_workorder(),
        "workorderCode": "WO-001",
        "quantity": quantity,
        "version": version,
    }


def _context(
    call_id: str,
    client: KTGClient,
    *,
    user_id: int,
    operation_id: str | None = None,
) -> ToolContext:
    return ToolContext(
        run_id="run-1",
        tool_call_id=call_id,
        operation_id=operation_id,
        identity={"userId": user_id, "userName": f"user-{user_id}"},
        ktg_client=client,
    )


def _product() -> dict[str, object]:
    return {
        "itemId": 11,
        "itemCode": "A-01",
        "itemName": "产品 A",
        "unitOfMeasure": "件",
        "itemOrProduct": "PRODUCT",
    }


def _saved_workorder() -> dict[str, object]:
    return {
        "workorderId": 51,
        "workorderCode": "WO-20261011-001",
        "workorderName": "产品 A 工单",
        "workorderType": "SELF",
        "orderSource": "MANUAL",
        "productId": 11,
        "productCode": "A-01",
        "productName": "产品 A",
        "unitOfMeasure": "件",
        "quantity": 100,
        "requestDate": "2026-10-31",
        "parentId": 0,
        "status": "PREPARE",
        "version": 0,
    }


def _preview_only_client() -> KTGClient:
    async def read_transport(
        url: str,
        _headers: Mapping[str, str],
        _params: Mapping[str, object],
        _timeout: float,
    ) -> object:
        if url.endswith("/mes/md/mditem/list"):
            return {"code": 200, "total": 1, "rows": [_product()]}
        if url.endswith("/system/autocode/get/WORKORDER_CODE"):
            return "WO-20261011-001"
        raise AssertionError(url)

    return KTGClient(
        base_url="http://127.0.0.1:8080",
        authorization="Bearer current-user-token",
        transport=read_transport,
    )
