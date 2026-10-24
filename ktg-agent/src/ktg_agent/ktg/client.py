"""Authenticated, fixed-origin client for the existing KTG service."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from decimal import Decimal, InvalidOperation
from collections.abc import Awaitable, Callable, Mapping
from http.client import HTTPException
from typing import cast
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ktg_agent.core.messages import JsonObject


class KTGError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


type KTGTransport = Callable[
    [str, Mapping[str, str], Mapping[str, object], float],
    Awaitable[object],
]
type KTGWriteTransport = Callable[
    [str, str, Mapping[str, str], Mapping[str, object], float],
    Awaitable[dict[str, object]],
]


class KTGClient:
    def __init__(
        self,
        *,
        base_url: str,
        authorization: str,
        timeout_seconds: float = 15.0,
        transport: KTGTransport | None = None,
        write_transport: KTGWriteTransport | None = None,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("KTG_BASE_URL must be an HTTP(S) URL")
        if not authorization.startswith("Bearer ") or not authorization[7:].strip():
            raise KTGError("AUTH_REQUIRED", "请先登录 KTG。", status=401)
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": authorization}
        self._timeout_seconds = timeout_seconds
        self._transport = transport or self._default_transport
        self._write_transport = write_transport or self._default_write_transport

    async def get_current_user(self) -> JsonObject:
        payload = await self.get("/getInfo")
        user = payload.get("user")
        if not isinstance(user, dict):
            raise KTGError("AUTH_INVALID", "KTG 未返回有效用户身份。", status=401)
        return cast(JsonObject, user)

    async def get(
        self, path: str, params: Mapping[str, object] | None = None
    ) -> dict[str, object]:
        self._validate_path(path)
        payload = await self._transport(
            f"{self._base_url}{path}",
            self._headers,
            params or {},
            self._timeout_seconds,
        )
        if not isinstance(payload, dict):
            raise KTGError("KTG_INVALID_RESPONSE", "KTG 返回结果不是对象。")
        checked = cast(dict[str, object], payload)
        self._raise_for_business_error(checked)
        return checked

    async def get_text(self, path: str) -> str:
        """Read one fixed KTG text endpoint, such as the auto-code service."""
        self._validate_path(path)
        payload = await self._transport(
            f"{self._base_url}{path}", self._headers, {}, self._timeout_seconds
        )
        if isinstance(payload, str) and payload.strip():
            return payload.strip()
        if isinstance(payload, dict):
            self._raise_for_business_error(cast(dict[str, object], payload))
            value = payload.get("data")
            if isinstance(value, str) and value.strip():
                return value.strip()
        raise KTGError("KTG_INVALID_RESPONSE", "KTG 未返回有效文本。")

    async def post(self, path: str, body: Mapping[str, object]) -> dict[str, object]:
        return await self._write("POST", path, body)

    async def put(self, path: str, body: Mapping[str, object]) -> dict[str, object]:
        return await self._write("PUT", path, body)

    async def delete(self, path: str) -> dict[str, object]:
        """Delete one resource at a fixed path on the configured KTG origin."""
        return await self._write("DELETE", path, {})

    @staticmethod
    def workorder_operation_digest(action: str, draft: Mapping[str, object]) -> str:
        """Return the MES-09 contract-v1 digest for a workorder operation."""
        if action not in {"CREATE_DRAFT", "EDIT_DRAFT"}:
            raise ValueError("Unsupported workorder operation action")
        normalized: dict[str, object] = {}
        integer_fields = {"productId", "parentId", "workorderId", "version", "clientId"}
        text_fields = {
            "workorderCode", "workorderName", "workorderType", "orderSource",
            "sourceCode", "batchCode", "clientCode", "clientName", "remark", "requestDate",
        }
        for key, value in draft.items():
            if value is None:
                continue
            if key == "quantity":
                if isinstance(value, bool):
                    raise ValueError("quantity must be numeric")
                try:
                    decimal = Decimal(str(value))
                except (InvalidOperation, ValueError):
                    raise ValueError("quantity must be numeric") from None
                if not decimal.is_finite():
                    raise ValueError("quantity must be finite")
                normalized[key] = format(decimal.normalize(), "f")
            elif key in integer_fields:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(f"{key} must be an integer")
                normalized[key] = value
            elif key in text_fields and isinstance(value, str):
                normalized[key] = value
            else:
                raise ValueError(f"Unsupported workorder draft field: {key}")
        canonical = json.dumps(
            {"action": action, "draft": normalized},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def workorder_operation_result(self, operation_id: str) -> dict[str, object]:
        normalized = str(uuid.UUID(operation_id))
        url = f"{self._base_url}/mes/pro/workorder/operations/{normalized}"
        payload = await self._transport(url, self._headers, {}, self._timeout_seconds)
        if not isinstance(payload, dict):
            raise KTGError("KTG_INVALID_RESPONSE", "工单回执无效，结果待核实。")
        if payload.get("code") in {401, 403}:
            self._raise_for_business_error(payload)
        data = payload.get("data")
        if isinstance(data, dict) and data.get("operationId") == normalized:
            return cast(dict[str, object], data)
        # A 404 means no receipt was found at this instant; the original write may
        # still be in flight, so callers must keep the result unknown.
        raise KTGError("OPERATION_RESULT_UNKNOWN", "工单操作回执暂不可用。", status=202)

    async def execute_workorder_operation(
        self, command: Mapping[str, object]
    ) -> dict[str, object]:
        """Submit and reconcile a durable operation without changing its ID."""
        raw_id = command.get("operationId")
        if not isinstance(raw_id, str):
            raise ValueError("Workorder execution requires an operationId")
        operation_id = str(uuid.UUID(raw_id))
        action = command.get("action")
        digest = command.get("requestDigest")
        draft = command.get("draft")
        if (not isinstance(action, str) or action not in {"CREATE_DRAFT", "EDIT_DRAFT"}
                or not isinstance(digest, str) or not isinstance(draft, Mapping)):
            raise ValueError("Invalid workorder operation command")
        if digest != self.workorder_operation_digest(action, draft):
            raise ValueError("Workorder operation digest does not match draft")
        body = {"operationId": operation_id, "action": action, "requestDigest": digest,
                "expectedVersion": command.get("expectedVersion"), "draft": dict(draft)}
        try:
            result = await self.workorder_operation_result(operation_id)
        except KTGError as exc:
            if exc.code in {"AUTH_EXPIRED", "PERMISSION_DENIED"}:
                raise
            result = None
        if result is None:
            # A missing receipt is not proof that an earlier request did not commit.
            # MES-09 makes replay safe only with this exact ID and exact payload.
            try:
                await self._write_transport(
                    "POST", f"{self._base_url}/mes/pro/workorder/operations",
                    self._headers, body, self._timeout_seconds,
                )
            except KTGError as exc:
                if exc.code in {"AUTH_EXPIRED", "PERMISSION_DENIED"}:
                    raise
            try:
                result = await self.workorder_operation_result(operation_id)
            except KTGError as exc:
                if exc.code in {"AUTH_EXPIRED", "PERMISSION_DENIED"}:
                    raise
                return {"operationId": operation_id, "outcome": "unknown", "safeToRetry": False}
        if (result.get("operationId") != operation_id or result.get("action") != action
                or result.get("requestDigest") != digest
                or result.get("expectedVersion") != command.get("expectedVersion")):
            return {"operationId": operation_id, "outcome": "failed",
                    "errorCode": "OPERATION_CONFLICT", "safeToRetry": False}
        if result.get("outcome") not in {"succeeded", "failed", "unknown"}:
            return {"operationId": operation_id, "outcome": "unknown", "safeToRetry": False}
        return result

    async def inventory_operation_result(self, operation_id: str) -> dict[str, object]:
        """Read the durable result using the caller's existing operation ID."""
        normalized = str(uuid.UUID(operation_id))
        payload = await self._transport(
            f"{self._base_url}/mes/wm/inventory-operations/{normalized}",
            self._headers, {}, self._timeout_seconds,
        )
        if not isinstance(payload, dict):
            raise KTGError("KTG_INVALID_RESPONSE", "库存回执无效，结果待核实。")
        if payload.get("code") in {401, 403}:
            self._raise_for_business_error(payload)
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("operationId") != normalized:
            raise KTGError("KTG_INVALID_RESPONSE", "库存回执无效，结果待核实。")
        if isinstance(payload.get("errorCode"), str):
            data["errorCode"] = payload["errorCode"]
        return cast(dict[str, object], data)

    async def execute_inventory_operation(
        self, command: Mapping[str, object]
    ) -> dict[str, object]:
        """Submit once, then confirm from storage. Never generate a replacement ID."""
        raw_id = command.get("operationId")
        if not isinstance(raw_id, str):
            raise ValueError("Inventory execution requires an operationId")
        operation_id = str(uuid.UUID(raw_id))
        try:
            await self.post("/mes/wm/inventory-operations", command)
        except KTGError as exc:
            if exc.code in {"AUTH_EXPIRED", "PERMISSION_DENIED"}:
                raise
        try:
            result = await self.inventory_operation_result(operation_id)
        except KTGError as exc:
            if exc.code in {"AUTH_EXPIRED", "PERMISSION_DENIED"}:
                raise
            return {"operationId": operation_id, "operationStatus": "unknown", "safeToRetry": False}
        if (result.get("actionType") != command.get("actionType")
                or result.get("documentId") != command.get("documentId")
                or result.get("operationStatus") not in {"succeeded", "failed", "rejected", "reconciling", "unknown"}):
            return {"operationId": operation_id, "operationStatus": "unknown", "safeToRetry": False}
        return result

    async def _write(
        self, method: str, path: str, body: Mapping[str, object]
    ) -> dict[str, object]:
        self._validate_path(path)
        payload = await self._write_transport(
            method,
            f"{self._base_url}{path}",
            self._headers,
            body,
            self._timeout_seconds,
        )
        if not isinstance(payload, dict):
            raise KTGError("KTG_INVALID_RESPONSE", "KTG 返回结果不是对象。")
        self._raise_for_business_error(payload)
        return payload

    @staticmethod
    def _validate_path(path: str) -> None:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("KTG path must be absolute and local to the configured host")

    @staticmethod
    def _raise_for_business_error(payload: Mapping[str, object]) -> None:
        code = payload.get("code", 200)
        if code != 200:
            message = payload.get("msg")
            if code == 401:
                raise KTGError("AUTH_EXPIRED", "登录状态已过期。", status=401)
            if code == 403:
                raise KTGError("PERMISSION_DENIED", "当前用户没有该查询权限。", status=403)
            if code == 409:
                raise KTGError(
                    "KTG_CONFLICT",
                    message if isinstance(message, str) else "工单状态或版本已变化。",
                    status=409,
                )
            raise KTGError(
                "KTG_REQUEST_FAILED",
                message if isinstance(message, str) else "KTG 请求失败。",
            )

    @staticmethod
    async def _default_transport(
        url: str,
        headers: Mapping[str, str],
        params: Mapping[str, object],
        timeout_seconds: float,
    ) -> object:
        return await asyncio.to_thread(
            KTGClient._get_payload,
            url,
            headers,
            params,
            timeout_seconds,
        )

    @staticmethod
    def _get_payload(
        url: str,
        headers: Mapping[str, str],
        params: Mapping[str, object],
        timeout_seconds: float,
    ) -> object:
        query = urlencode([(key, str(value)) for key, value in params.items()])
        target = f"{url}?{query}" if query else url
        request = Request(target, headers=dict(headers), method="GET")
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read()
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise KTGError(
                    "AUTH_EXPIRED" if exc.code == 401 else "PERMISSION_DENIED",
                    "登录状态已过期。" if exc.code == 401 else "当前用户没有该查询权限。",
                    status=exc.code,
                ) from exc
            if exc.code in {202, 404, 409, 422, 500} and (
                "/mes/wm/inventory-operations/" in url
                or "/mes/pro/workorder/operations/" in url
            ):
                return KTGClient._decode_payload(exc.read())
            raise KTGError("KTG_HTTP_ERROR", f"KTG 接口返回 HTTP {exc.code}。") from exc
        except (OSError, HTTPException) as exc:
            raise KTGError("KTG_CONNECTION_ERROR", "无法连接 KTG 服务。") from exc
        return KTGClient._decode_payload(raw)

    @staticmethod
    async def _default_write_transport(
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, object],
        timeout_seconds: float,
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            KTGClient._send_json, method, url, headers, body, timeout_seconds
        )

    @staticmethod
    def _send_json(
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, object],
        timeout_seconds: float,
    ) -> dict[str, object]:
        request_headers = {**headers, "Content-Type": "application/json;charset=utf-8"}
        request = Request(
            url,
            headers=request_headers,
            data=json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            method=method,
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read()
        except HTTPError as exc:
            KTGClient._raise_http_error(exc)
        except (OSError, HTTPException) as exc:
            raise KTGError("KTG_CONNECTION_ERROR", "无法连接 KTG 服务。") from exc
        value = KTGClient._decode_payload(raw)
        if not isinstance(value, dict):
            raise KTGError("KTG_INVALID_RESPONSE", "KTG 返回结果不是对象。")
        return cast(dict[str, object], value)

    @staticmethod
    def _decode_payload(raw: bytes) -> object:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise KTGError("KTG_INVALID_RESPONSE", "KTG 返回了无效数据。") from exc
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            if text.strip():
                return text.strip()
            raise KTGError("KTG_INVALID_RESPONSE", "KTG 返回了无效数据。")

    @staticmethod
    def _raise_http_error(exc: HTTPError) -> None:
        if exc.code in {401, 403}:
            raise KTGError(
                "AUTH_EXPIRED" if exc.code == 401 else "PERMISSION_DENIED",
                "登录状态已过期。" if exc.code == 401 else "当前用户没有该操作权限。",
                status=exc.code,
            ) from exc
        if exc.code == 409:
            raise KTGError("KTG_CONFLICT", "工单状态或版本已变化。", status=409) from exc
        raise KTGError("KTG_HTTP_ERROR", f"KTG 接口返回 HTTP {exc.code}。") from exc

