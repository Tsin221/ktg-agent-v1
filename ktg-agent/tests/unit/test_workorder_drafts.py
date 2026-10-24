import asyncio
from collections.abc import Mapping
from jsonschema import Draft202012Validator

from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.ktg.workorders import (
    PreviewError,
    PreviewStore,
    _matches_request,
    _resolve_product,
    _verified_result,
    _quantity,
    build_workorder_tools,
)
from ktg_agent.tools import ToolContext, ToolStatus


def test_create_preview_resolves_product_and_confirmation_writes_once() -> None:
    asyncio.run(_create_preview_resolves_product_and_confirmation_writes_once())


async def _create_preview_resolves_product_and_confirmation_writes_once() -> None:
    writes: list[tuple[str, str, Mapping[str, object]]] = []
    receipt: dict[str, object] | None = None

    async def read_transport(
        url: str,
        _headers: Mapping[str, str],
        params: Mapping[str, object],
        _timeout: float,
    ) -> object:
        if "/mes/pro/workorder/operations/" in url:
            return {"code": 200, "data": receipt} if receipt else {"code": 404}
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
            return {"code": 200, "data": {**_saved_workorder(),
                "workorderName": "产品 A 订单工单", "orderSource": "ORDER"}}
        if url.endswith("/mes/md/bom/list"):
            return {"code": 200, "total": 1,
                    "rows": [{"bomItemId": 101, "quantity": 2}]}
        if url.endswith("/mes/pro/workorderbom/list"):
            assert params["workorderId"] == 51
            return {"code": 200, "total": 1,
                    "rows": [{"workorderId": 51, "itemId": 101, "quantity": 200}]}
        raise AssertionError(url)

    async def write_transport(
        method: str,
        url: str,
        _headers: Mapping[str, str],
        body: Mapping[str, object],
        _timeout: float,
    ) -> dict[str, object]:
        nonlocal receipt
        writes.append((method, url, body))
        receipt = {"operationId": body["operationId"], "action": body["action"],
                   "requestDigest": body["requestDigest"], "expectedVersion": body["expectedVersion"],
                   "outcome": "succeeded", "workorderId": 51, "workorderCode": "WO-20261011-001"}
        return {"code": 200, "data": receipt}

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
            "code_mode": "auto", "workorder_name": "产品 A 订单工单",
            "order_source": "ORDER", "workorder_type": "SELF",
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
        "specification": None,
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
    assert url.endswith("/mes/pro/workorder/operations")
    assert body["operationId"] == preview_id
    assert body["action"] == "CREATE_DRAFT"
    assert body["expectedVersion"] is None
    draft = body["draft"]
    assert isinstance(draft, Mapping)
    assert body["requestDigest"] == KTGClient.workorder_operation_digest(
        "CREATE_DRAFT", draft
    )
    assert draft == {
        "workorderCode": "WO-20261011-001", "workorderName": "产品 A 订单工单",
        "workorderType": "SELF", "orderSource": "ORDER", "productId": 11,
        "quantity": "100", "requestDate": "2026-10-31", "parentId": 0,
    }


def test_create_schema_requires_business_fields_and_manual_code() -> None:
    tool = next(tool for tool in build_workorder_tools(PreviewStore())
                if tool.name == "ktg_preview_create_workorder_draft")
    assert tool.input_schema is not None
    valid = {"code_mode": "manual", "workorder_code": "WO-MANUAL",
             "workorder_name": "用户指定名称", "order_source": "STORE",
             "quantity": 1, "request_date": "2026-10-31", "product_code": "A-01"}
    assert not list(Draft202012Validator(tool.input_schema).iter_errors(valid))
    for missing in ("workorder_name", "order_source", "quantity", "request_date", "product_code"):
        invalid = dict(valid)
        invalid.pop(missing)
        assert list(Draft202012Validator(tool.input_schema).iter_errors(invalid)), missing
    invalid = dict(valid)
    invalid.pop("workorder_code")
    assert list(Draft202012Validator(tool.input_schema).iter_errors(invalid))
    for quantity in (0.29, 0.57, "0.1", "0.10", "1.2300"):
        candidate = {**valid, "quantity": quantity}
        assert not list(Draft202012Validator(tool.input_schema).iter_errors(candidate)), quantity


def test_quantity_obeys_screen_range_and_database_precision_without_rounding() -> None:
    assert _quantity("0.01") == "0.01"
    assert _quantity("99999999") == "99999999"
    assert _quantity(0.29) == "0.29"
    assert _quantity(0.57) == "0.57"
    assert _quantity("0.1") == "0.1"
    assert _quantity("0.10") == "0.1"
    assert _quantity("1.2300") == "1.23"
    for invalid in ("0.009", "100000000", "0.011"):
        try:
            _quantity(invalid)
        except PreviewError as error:
            assert error.code == "INVALID_QUANTITY"
        else:
            raise AssertionError(f"accepted invalid quantity {invalid}")


def test_omitted_workorder_type_uses_web_default_self_in_saved_preview() -> None:
    async def run() -> None:
        tool = next(tool for tool in build_workorder_tools(PreviewStore())
                    if tool.name == "ktg_preview_create_workorder_draft")
        result = await tool.execute(_context("preview-default-self", _preview_only_client(), user_id=7), {
            "code_mode": "auto", "workorder_name": "默认类型测试", "order_source": "STORE",
            "product_code": "A-01", "quantity": 1, "request_date": "2026-10-31",
        })
        assert result.status is ToolStatus.SUCCEEDED
        assert result.details is not None
        assert result.details["workorderType"] == "SELF"
        changes = result.details["changes"]
        assert isinstance(changes, list)
        assert any(change.get("field") == "workorderType" and change.get("after") == "SELF"
                   for change in changes if isinstance(change, Mapping))
    asyncio.run(run())


def test_preview_rejects_ambiguous_product_without_creating_approval() -> None:
    asyncio.run(_preview_rejects_ambiguous_product_without_creating_approval())


def test_product_code_never_accepts_a_partial_match() -> None:
    async def read_transport(url: str, _headers: Mapping[str, str],
                             _params: Mapping[str, object], _timeout: float) -> object:
        assert url.endswith("/mes/md/mditem/list")
        return {"code": 200, "total": 1,
                "rows": [{"itemId": 7, "itemCode": "XA-01-Z", "itemName": "其他产品"}]}

    client = KTGClient(base_url="http://127.0.0.1:8080",
                       authorization="Bearer current-user-token", transport=read_transport)
    async def check() -> None:
        try:
            await _resolve_product(client, {"product_code": "A-01"})
        except PreviewError as exc:
            assert exc.code == "PRODUCT_NOT_FOUND"
        else:
            raise AssertionError("partial code was accepted")
    asyncio.run(check())


def test_product_id_uses_detail_instead_of_first_list_page() -> None:
    async def read_transport(url: str, _headers: Mapping[str, str],
                             _params: Mapping[str, object], _timeout: float) -> object:
        assert url.endswith("/mes/md/mditem/25")
        return {"code": 200, "data": {"itemId": 25, "itemCode": "P-25",
                                          "itemName": "第二页产品"}}

    client = KTGClient(base_url="http://127.0.0.1:8080",
                       authorization="Bearer current-user-token", transport=read_transport)
    product = asyncio.run(_resolve_product(client, {"product_id": 25}))
    assert product["itemId"] == 25

    async def mismatched_code() -> None:
        try:
            await _resolve_product(client, {"product_id": 25, "product_code": "P-26"})
        except PreviewError as exc:
            assert exc.code == "PRODUCT_MISMATCH"
        else:
            raise AssertionError("conflicting product identifiers were accepted")
    asyncio.run(mismatched_code())


def test_product_name_checks_later_pages_for_duplicates() -> None:
    async def read_transport(url: str, _headers: Mapping[str, str],
                             params: Mapping[str, object], _timeout: float) -> object:
        assert url.endswith("/mes/md/mditem/list")
        if params["pageNum"] == 1:
            return {"code": 200, "total": 21,
                    "rows": [{"itemId": 1, "itemCode": "P-1", "itemName": "产品 A"}]
                    + [{"itemId": i, "itemCode": f"P-{i}", "itemName": "产品 A 零件"}
                       for i in range(2, 21)]}
        return {"code": 200, "total": 21,
                "rows": [{"itemId": 21, "itemCode": "P-21", "itemName": "产品 A"}]}

    client = KTGClient(base_url="http://127.0.0.1:8080",
                       authorization="Bearer current-user-token", transport=read_transport)
    async def check() -> None:
        try:
            await _resolve_product(client, {"product_name": "产品 A"})
        except PreviewError as exc:
            assert exc.code == "AMBIGUOUS_PRODUCT"
        else:
            raise AssertionError("later duplicate was ignored")
    asyncio.run(check())


def test_verification_rejects_a_different_remark() -> None:
    request = {"workorderCode": "WO-1", "workorderName": "工单", "workorderType": "SELF",
               "orderSource": "MANUAL", "productId": 11, "parentId": 0,
               "quantity": "1", "requestDate": "2026-10-31", "remark": "新备注"}
    detail: dict[object, object] = {}
    detail.update(request)
    detail.update({"status": "PREPARE", "remark": "旧备注"})
    assert not _matches_request(detail, request)


def test_verification_rejects_an_uncleared_batch_code() -> None:
    request = {"workorderCode": "WO-1", "workorderName": "工单", "workorderType": "SELF",
               "orderSource": "MANUAL", "productId": 11, "parentId": 0,
               "quantity": "1", "requestDate": "2026-10-31", "batchCode": ""}
    detail: dict[object, object] = {}
    detail.update(request)
    detail.update({"status": "PREPARE", "batchCode": "OLD-BATCH"})
    assert not _matches_request(detail, request)


def test_create_bom_verification_rejects_missing_required_line() -> None:
    async def read_transport(url: str, _headers: Mapping[str, str],
                             _params: Mapping[str, object], _timeout: float) -> object:
        if url.endswith("/mes/pro/workorder/51"):
            return {"code": 200, "data": _saved_workorder()}
        if url.endswith("/mes/md/bom/list"):
            return {"code": 200, "total": 1,
                    "rows": [{"bomItemId": 101, "quantity": 2}]}
        if url.endswith("/mes/pro/workorderbom/list"):
            return {"code": 200, "total": 0, "rows": []}
        raise AssertionError(url)

    client = KTGClient(base_url="http://127.0.0.1:8080",
                       authorization="Bearer current-user-token", transport=read_transport)
    request = {"workorderCode": "WO-20261011-001", "workorderName": "产品 A 工单",
               "workorderType": "SELF", "orderSource": "MANUAL", "productId": 11,
               "parentId": 0, "quantity": "100", "requestDate": "2026-10-31"}
    async def check() -> None:
        try:
            await _verified_result(client, "create", request, 51, "preview", reconciled=False)
        except PreviewError as exc:
            assert exc.code == "BOM_VERIFICATION_FAILED"
        else:
            raise AssertionError("missing BOM line was reported as verified")
    asyncio.run(check())


def test_bom_verification_accepts_mysql_two_decimal_storage() -> None:
    async def read_transport(url: str, _headers: Mapping[str, str],
                             _params: Mapping[str, object], _timeout: float) -> object:
        if url.endswith("/mes/pro/workorder/51"):
            return {"code": 200, "data": {**_saved_workorder(), "quantity": 12.01}}
        if url.endswith("/mes/md/bom/list"):
            return {"code": 200, "total": 1,
                    "rows": [{"bomItemId": 101, "quantity": 0.5}]}
        if url.endswith("/mes/pro/workorderbom/list"):
            return {"code": 200, "total": 1,
                    "rows": [{"itemId": 101, "quantity": 6.0}]}
        raise AssertionError(url)

    client = KTGClient(base_url="http://127.0.0.1:8080",
                       authorization="Bearer current-user-token", transport=read_transport)
    request = {"workorderCode": "WO-20261011-001", "workorderName": "产品 A 工单",
               "workorderType": "SELF", "orderSource": "MANUAL", "productId": 11,
               "parentId": 0, "quantity": "12.01", "requestDate": "2026-10-31"}
    result = asyncio.run(_verified_result(client, "create", request, 51, "preview",
                                          reconciled=False))
    assert result.details is not None
    assert result.details["bom_verified"] is True


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
            "code_mode": "auto", "workorder_name": "歧义产品测试",
            "order_source": "STORE", "workorder_type": "SELF",
            "product_name": "产品 A",
            "quantity": 100,
            "request_date": "2026-10-31",
        },
    )

    assert result.status is ToolStatus.FAILED
    assert result.error_code == "AMBIGUOUS_PRODUCT"
    assert "A-01" in result.content and "A-02" in result.content
    assert "无法确定唯一产品" in result.content
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
            "code_mode": "auto", "workorder_name": "用户绑定测试",
            "order_source": "STORE", "workorder_type": "SELF",
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
    operation_ids: list[str] = []

    async def read_transport(
        url: str,
        _headers: Mapping[str, str],
        _params: Mapping[str, object],
        _timeout: float,
    ) -> object:
        if "/mes/pro/workorder/operations/" in url:
            return {"code": 404}
        if url.endswith("/mes/md/mditem/list"):
            return {"code": 200, "total": 1, "rows": [_product()]}
        if url.endswith("/system/autocode/get/WORKORDER_CODE"):
            return "WO-20261011-001"
        if url.endswith("/mes/pro/workorder/list"):
            return {"code": 200, "total": 1, "rows": [_saved_workorder()]}
        if url.endswith("/mes/pro/workorder/51"):
            return {"code": 200, "data": _saved_workorder()}
        if url.endswith("/mes/md/bom/list"):
            return {"code": 200, "total": 0, "rows": []}
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
            "code_mode": "auto", "workorder_name": "超时重试测试",
            "order_source": "STORE", "workorder_type": "SELF",
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

    assert result.status is ToolStatus.UNKNOWN
    assert result.details is not None and result.details["outcome"] == "unknown"
    assert write_attempts == 1


def test_create_timeout_with_failed_reconciliation_stays_unknown_without_retry() -> None:
    asyncio.run(_create_timeout_with_failed_reconciliation_stays_unknown_without_retry())


async def _create_timeout_with_failed_reconciliation_stays_unknown_without_retry() -> None:
    write_attempts = 0
    operation_ids: list[str] = []

    async def read_transport(
        url: str,
        _headers: Mapping[str, str],
        _params: Mapping[str, object],
        _timeout: float,
    ) -> object:
        if "/mes/pro/workorder/operations/" in url:
            return {"code": 404}
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
        body: Mapping[str, object],
        _timeout: float,
    ) -> dict[str, object]:
        nonlocal write_attempts
        write_attempts += 1
        operation_ids.append(str(body["operationId"]))
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
            "code_mode": "auto", "workorder_name": "未知结果测试",
            "order_source": "STORE", "workorder_type": "SELF",
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
    assert write_attempts == 2
    assert operation_ids == [preview_id, preview_id]


def test_edit_preview_merges_allowed_change_and_checks_version_before_write() -> None:
    asyncio.run(_edit_preview_merges_allowed_change_and_checks_version_before_write())


async def _edit_preview_merges_allowed_change_and_checks_version_before_write() -> None:
    detail_reads = 0
    written_body: Mapping[str, object] | None = None
    receipt: dict[str, object] | None = None

    async def read_transport(
        url: str,
        _headers: Mapping[str, str],
        _params: Mapping[str, object],
        _timeout: float,
    ) -> object:
        nonlocal detail_reads
        if "/mes/pro/workorder/operations/" in url:
            return {"code": 200, "data": receipt} if receipt else {"code": 404}
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
        if url.endswith("/mes/md/mditem/11"):
            return {"code": 200, "data": _product()}
        if url.endswith("/mes/md/bom/list"):
            return {"code": 200, "total": 1,
                    "rows": [{"bomItemId": 101, "quantity": 2}]}
        if url.endswith("/mes/pro/workorderbom/list"):
            return {"code": 200, "total": 1,
                    "rows": [{"itemId": 101, "quantity": 240}]}
        raise AssertionError(url)

    async def write_transport(
        method: str,
        url: str,
        _headers: Mapping[str, str],
        body: Mapping[str, object],
        _timeout: float,
    ) -> dict[str, object]:
        nonlocal written_body, receipt
        assert method == "POST"
        assert url.endswith("/mes/pro/workorder/operations")
        written_body = body
        receipt = {"operationId": body["operationId"], "action": body["action"],
                   "requestDigest": body["requestDigest"], "expectedVersion": body["expectedVersion"],
                   "outcome": "succeeded", "workorderId": 51, "workorderCode": "WO-001"}
        return {"code": 200, "data": receipt}

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
    assert written_body["operationId"] == preview_id
    assert written_body["action"] == "EDIT_DRAFT"
    assert written_body["expectedVersion"] == 3
    draft = written_body["draft"]
    assert isinstance(draft, Mapping)
    assert draft["workorderId"] == 51
    assert draft["version"] == 3
    assert draft["quantity"] == "120"
    assert "status" not in draft


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


def test_persistent_preview_store_survives_agent_store_reconstruction() -> None:
    from pathlib import Path
    from uuid import uuid4

    path = Path(".runtime") / f"mes10-{uuid4()}.sqlite3"
    first = PreviewStore(path=path)
    from ktg_agent.core.messages import JsonObject
    from typing import cast

    draft = cast(JsonObject, {"workorderCode": "WO-PERSIST", "version": 4})
    preview_id = first.create(user_key="id:7", action="edit", request=draft,
                              preview={"kind": "workorder_draft_preview"})
    second = PreviewStore(path=path)
    assert second.action_for(preview_id=preview_id, user_key="id:7") == "edit"
    record, cached = second.begin(preview_id=preview_id, user_key="id:7", action="edit")
    assert cached is None
    assert record.preview_id == preview_id
    assert record.request == draft
    assert record.request["version"] == 4
    try:
        second.action_for(preview_id=preview_id, user_key="id:8")
    except PreviewError as error:
        assert error.code == "APPROVAL_IDENTITY_MISMATCH"
    else:
        raise AssertionError("another user read the persisted preview")
    path.unlink(missing_ok=True)


def test_persistent_preview_expires_and_unknown_result_can_resume_same_id() -> None:
    from pathlib import Path
    from time import sleep
    from uuid import uuid4

    path = Path(".runtime") / f"mes10-expire-{uuid4()}.sqlite3"
    try:
        expiring = PreviewStore(path=path, ttl_seconds=0.01)
        expired_id = expiring.create(user_key="id:7", action="create", request={}, preview={})
        sleep(0.02)
        try:
            PreviewStore(path=path).action_for(preview_id=expired_id, user_key="id:7")
        except PreviewError as error:
            assert error.code == "APPROVAL_EXPIRED"
        else:
            raise AssertionError("expired proposal remained confirmable")
    finally:
        path.unlink(missing_ok=True)

    path = Path(".runtime") / f"mes10-completed-expire-{uuid4()}.sqlite3"
    try:
        from ktg_agent.tools import ToolResult

        expiring = PreviewStore(path=path, ttl_seconds=0.01)
        completed_id = expiring.create(
            user_key="id:7", action="create", request={}, preview={})
        completed = ToolResult(content="已完成", status=ToolStatus.SUCCEEDED,
                               operation_id=completed_id)
        expiring.finish(completed_id, completed)
        sleep(0.02)

        restored = PreviewStore(path=path, ttl_seconds=0.01)
        assert restored.action_for(preview_id=completed_id, user_key="id:7") == "create"
        record, cached = restored.begin(
            preview_id=completed_id, user_key="id:7", action="create")
        assert record.state == "completed"
        assert cached is not None and cached.content == "已完成"
    finally:
        path.unlink(missing_ok=True)

    path = Path(".runtime") / f"mes10-unknown-{uuid4()}.sqlite3"
    try:
        store = PreviewStore(path=path)
        preview_id = store.create(user_key="id:7", action="create", request={
            "workorderCode": "WO-UNKNOWN"}, preview={})
        unknown = ToolStatus.UNKNOWN
        from ktg_agent.tools import ToolResult
        store.finish(preview_id, ToolResult(
            content="待核实", status=unknown, error_code="WRITE_RESULT_UNKNOWN",
            operation_id=preview_id,
        ))
        resumed, cached = PreviewStore(path=path).begin(
            preview_id=preview_id, user_key="id:7", action="create")
        assert resumed.preview_id == preview_id
        assert cached is None
    finally:
        path.unlink(missing_ok=True)


def test_persistent_preview_rejects_changed_digest_and_expected_version() -> None:
    from pathlib import Path
    import sqlite3
    from uuid import uuid4
    from ktg_agent.core.messages import JsonObject
    from typing import cast

    path = Path(".runtime") / f"mes10-binding-{uuid4()}.sqlite3"
    try:
        store = PreviewStore(path=path)
        request = cast(JsonObject, {
            "workorderId": 51, "version": 4, "workorderCode": "WO-EDIT",
            "workorderName": "Edit", "workorderType": "SELF", "orderSource": "MANUAL",
            "productId": 11, "quantity": "10", "requestDate": "2026-10-31", "parentId": 0,
        })
        preview_id = store.create(user_key="id:7", action="edit", request=request, preview={})
        db = sqlite3.connect(path)
        try:
            with db:
                db.execute("UPDATE workorder_previews SET expected_version=5 WHERE preview_id=?", (preview_id,))
        finally:
            db.close()
        try:
            PreviewStore(path=path).begin(preview_id=preview_id, user_key="id:7", action="edit")
        except PreviewError as error:
            assert error.code == "APPROVAL_CHANGED"
        else:
            raise AssertionError("changed expected version was accepted")
    finally:
        path.unlink(missing_ok=True)
