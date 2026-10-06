"""Authenticated read-only client for the existing KTG service."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import cast
from urllib.error import HTTPError, URLError
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
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("KTG_BASE_URL must be an HTTP(S) URL")
        if not authorization.startswith("Bearer ") or not authorization[7:].strip():
            raise KTGError("AUTH_REQUIRED", "请先登录 KTG。", status=401)
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": authorization}
        self._timeout_seconds = timeout_seconds
        self._transport = transport or self._default_transport

    async def get_current_user(self) -> JsonObject:
        payload = await self.get("/getInfo")
        user = payload.get("user")
        if not isinstance(user, dict):
            raise KTGError("AUTH_INVALID", "KTG 未返回有效用户身份。", status=401)
        return cast(JsonObject, user)

    async def get(
        self, path: str, params: Mapping[str, object] | None = None
    ) -> dict[str, object]:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("KTG path must be absolute and local to the configured host")
        payload = await self._transport(
            f"{self._base_url}{path}",
            self._headers,
            params or {},
            self._timeout_seconds,
        )
        code = payload.get("code", 200)
        if code != 200:
            message = payload.get("msg")
            if code == 401:
                raise KTGError("AUTH_EXPIRED", "登录状态已过期。", status=401)
            if code == 403:
                raise KTGError("PERMISSION_DENIED", "当前用户没有该查询权限。", status=403)
            raise KTGError(
                "KTG_REQUEST_FAILED",
                message if isinstance(message, str) else "KTG 查询失败。",
            )
        return payload

    @staticmethod
    async def _default_transport(
        url: str,
        headers: Mapping[str, str],
        params: Mapping[str, object],
        timeout_seconds: float,
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            KTGClient._get_json,
            url,
            headers,
            params,
            timeout_seconds,
        )

    @staticmethod
    def _get_json(
        url: str,
        headers: Mapping[str, str],
        params: Mapping[str, object],
        timeout_seconds: float,
    ) -> dict[str, object]:
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
            raise KTGError("KTG_HTTP_ERROR", f"KTG 接口返回 HTTP {exc.code}。") from exc
        except (URLError, TimeoutError) as exc:
            raise KTGError("KTG_CONNECTION_ERROR", "无法连接 KTG 服务。") from exc
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise KTGError("KTG_INVALID_RESPONSE", "KTG 返回了无效数据。") from exc
        if not isinstance(value, dict):
            raise KTGError("KTG_INVALID_RESPONSE", "KTG 返回结果不是对象。")
        return cast(dict[str, object], value)

