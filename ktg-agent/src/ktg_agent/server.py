"""Minimal local HTTP entry point for the KTG Agent query panel."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from ktg_agent.core.messages import JsonObject
from ktg_agent.ktg import KTGClient, KTGError
from ktg_agent.ktg.inventory import ISSUE, RECEIPT, InventoryStore
from ktg_agent.providers import OpenAICompatibleProvider
from ktg_agent.providers.base import ModelConfig, ProviderError
from ktg_agent.service import QueryService
from ktg_agent.sessions import SessionRepository


@dataclass(frozen=True, slots=True)
class Settings:
    model_api_key: str
    model_base_url: str
    model_id: str
    ktg_base_url: str
    host: str = "127.0.0.1"
    port: int = 8001
    inventory_db_path: str = ""
    session_db_path: str = ""
    enable_item_receipt: bool = False
    enable_production_issue: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        api_key = os.environ.get("MODEL_API_KEY", "").strip()
        model_id = os.environ.get("MODEL_ID", "").strip()
        if not api_key or api_key == "CHANGE_ME":
            raise RuntimeError("MODEL_API_KEY must be configured")
        if not model_id or model_id == "CHANGE_ME":
            raise RuntimeError("MODEL_ID must be configured")
        model_base_url = os.environ.get(
            "MODEL_BASE_URL", "https://api.openai.com/v1"
        ).strip()
        ktg_base_url = os.environ.get(
            "KTG_BASE_URL", "http://127.0.0.1:8080"
        ).strip()
        host = os.environ.get("AGENT_HOST", "127.0.0.1").strip()
        for name, value in (
            ("MODEL_BASE_URL", model_base_url),
            ("KTG_BASE_URL", ktg_base_url),
            ("AGENT_HOST", host),
        ):
            if not value:
                raise RuntimeError(f"{name} must not be blank")
        raw_port = os.environ.get("AGENT_PORT", "8001")
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise RuntimeError("AGENT_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise RuntimeError("AGENT_PORT must be between 1 and 65535")
        return cls(
            model_api_key=api_key,
            model_base_url=model_base_url,
            model_id=model_id,
            ktg_base_url=ktg_base_url,
            host=host,
            port=port,
            inventory_db_path=os.environ.get("AGENT_INVENTORY_DB", "").strip(),
            session_db_path=os.environ.get("AGENT_SESSION_DB", "").strip(),
            enable_item_receipt=os.environ.get("AGENT_ENABLE_ITEM_RECEIPT", "false").lower() == "true",
            enable_production_issue=os.environ.get("AGENT_ENABLE_PRODUCTION_ISSUE", "false").lower() == "true",
        )


def create_session_repository(settings: Settings) -> SessionRepository:
    """Open the separately configured, versioned conversation database."""
    session_path = Path(settings.session_db_path)
    if not settings.session_db_path or not session_path.is_absolute():
        raise RuntimeError("AGENT_SESSION_DB must be an absolute path")
    if settings.inventory_db_path and session_path.resolve() == Path(settings.inventory_db_path).resolve():
        raise RuntimeError("AGENT_SESSION_DB must differ from AGENT_INVENTORY_DB")
    return SessionRepository(session_path)


def create_service(settings: Settings) -> QueryService:
    provider = OpenAICompatibleProvider(
        api_key=settings.model_api_key,
        base_url=settings.model_base_url,
    )

    def client_factory(authorization: str) -> KTGClient:
        return KTGClient(
            base_url=settings.ktg_base_url,
            authorization=authorization,
        )

    actions = frozenset(
        action for action, enabled in (
            (RECEIPT, settings.enable_item_receipt),
            (ISSUE, settings.enable_production_issue),
        ) if enabled
    )
    if actions and not Path(settings.inventory_db_path).is_absolute():
        raise RuntimeError("AGENT_INVENTORY_DB must be an absolute path when inventory actions are enabled")
    return QueryService(
        provider=provider,
        model_config=ModelConfig(
            model=settings.model_id,
            max_output_tokens=1024,
        ),
        ktg_client_factory=client_factory,
        inventory_store=InventoryStore(settings.inventory_db_path) if actions else None,
        inventory_actions=actions,
    )


class AgentHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: QueryService) -> None:
        super().__init__(address, AgentRequestHandler)
        self.query_service = service


class AgentRequestHandler(BaseHTTPRequestHandler):
    server: AgentHTTPServer

    def do_GET(self) -> None:
        if self.path.startswith("/agent-api/v1/inventory-operations/"):
            operation_id = self.path.rsplit("/", 1)[-1]
            try:
                response = asyncio.run(self.server.query_service.get_inventory_result(
                    operation_id=operation_id,
                    authorization=self.headers.get("Authorization", ""),
                ))
            except KTGError as exc:
                self._write_error(HTTPStatus(exc.status), exc.code, str(exc))
                return
            except ValueError as exc:
                self._write_error(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", str(exc))
                return
            self._write_json(HTTPStatus.OK, response)
            return
        if self.path == "/health":
            self._write_json(HTTPStatus.OK, {"status": "ok"})
            return
        self._write_json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})

    def do_POST(self) -> None:
        if self.path not in {
            "/agent-api/v1/query",
            "/agent-api/v1/workorder-drafts/confirm",
            "/agent-api/v1/inventory-operations/confirm",
        }:
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 64 * 1024:
            self._write_error(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "请求内容无效。")
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._write_error(HTTPStatus.BAD_REQUEST, "INVALID_JSON", "请求不是有效 JSON。")
            return
        if not isinstance(payload, dict):
            self._write_error(HTTPStatus.BAD_REQUEST, "VALIDATION_ERROR", "请求必须是对象。")
            return
        authorization = self.headers.get("Authorization", "")
        try:
            if self.path == "/agent-api/v1/query":
                message = payload.get("message")
                if not isinstance(message, str):
                    raise ValueError("message 必须是文本。")
                response = asyncio.run(
                    self.server.query_service.query(
                        message=message,
                        authorization=authorization,
                    )
                )
            elif self.path == "/agent-api/v1/workorder-drafts/confirm":
                preview_id = payload.get("preview_id")
                if not isinstance(preview_id, str):
                    raise ValueError("preview_id 必须是文本。")
                response = asyncio.run(
                    self.server.query_service.confirm(
                        preview_id=preview_id,
                        authorization=authorization,
                    )
                )
            else:
                operation_id = payload.get("operation_id")
                if not isinstance(operation_id, str):
                    raise ValueError("operation_id 必须是文本。")
                response = asyncio.run(self.server.query_service.confirm_inventory(
                    operation_id=operation_id, authorization=authorization,
                ))
        except KTGError as exc:
            self._write_error(HTTPStatus(exc.status), exc.code, str(exc))
            return
        except ProviderError as exc:
            self._write_error(HTTPStatus.BAD_GATEWAY, exc.code, str(exc))
            return
        except ValueError as exc:
            self._write_error(HTTPStatus.BAD_REQUEST, "VALIDATION_ERROR", str(exc))
            return
        except Exception:
            self._write_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "INTERNAL_ERROR",
                "Agent 请求失败，请稍后重试。",
            )
            return
        self._write_json(HTTPStatus.OK, response)

    def log_message(self, format: str, *args: object) -> None:
        # Keep request logs free of headers, tokens and prompt bodies.
        super().log_message(format, *args)

    def _write_error(self, status: HTTPStatus, code: str, message: str) -> None:
        self._write_json(status, {"error": {"code": code, "message": message}})

    def _write_json(self, status: HTTPStatus, payload: JsonObject) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    settings = Settings.from_env()
    server = AgentHTTPServer((settings.host, settings.port), create_service(settings))
    print(f"KTG Agent listening on http://{settings.host}:{settings.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

