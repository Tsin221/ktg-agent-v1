"""Minimal local HTTP entry point for the KTG Agent query panel."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ktg_agent.core.messages import JsonObject
from ktg_agent.ktg import KTGClient, KTGError
from ktg_agent.providers import OpenAICompatibleProvider
from ktg_agent.providers.base import ModelConfig, ProviderError
from ktg_agent.service import QueryService


@dataclass(frozen=True, slots=True)
class Settings:
    model_api_key: str
    model_base_url: str
    model_id: str
    ktg_base_url: str
    host: str = "127.0.0.1"
    port: int = 8001

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
        )


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

    return QueryService(
        provider=provider,
        model_config=ModelConfig(
            model=settings.model_id,
            max_output_tokens=1024,
        ),
        ktg_client_factory=client_factory,
    )


class AgentHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: QueryService) -> None:
        super().__init__(address, AgentRequestHandler)
        self.query_service = service


class AgentRequestHandler(BaseHTTPRequestHandler):
    server: AgentHTTPServer

    def do_GET(self) -> None:
        if self.path != "/health":
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})
            return
        self._write_json(HTTPStatus.OK, {"status": "ok"})

    def do_POST(self) -> None:
        if self.path != "/agent-api/v1/query":
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
        message = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(message, str):
            self._write_error(HTTPStatus.BAD_REQUEST, "VALIDATION_ERROR", "message 必须是文本。")
            return
        authorization = self.headers.get("Authorization", "")
        try:
            response = asyncio.run(
                self.server.query_service.query(
                    message=message,
                    authorization=authorization,
                )
            )
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
                "Agent 查询失败，请稍后重试。",
            )
            return
        self._write_json(HTTPStatus.OK, response)

    def log_message(self, format: str, *args: object) -> None:
        # Keep request logs free of headers, tokens and prompt bodies.
        super().log_message(format, *args)

    def _write_error(self, status: HTTPStatus, code: str, message: str) -> None:
        self._write_json(status, {"error": {"code": code, "message": message}})

    def _write_json(self, status: HTTPStatus, payload: JsonObject) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
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

