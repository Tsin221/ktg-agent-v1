"""Candidate FastAPI foundation. Importing or constructing an app acquires no resources."""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send, Message

from ktg_agent.http_runs import Core, Events, RunRuntime
from ktg_agent.ktg.client import KTGError
from ktg_agent.providers.base import ProviderError
from ktg_agent.server import Settings, create_session_repository
from ktg_agent.sessions.repository import SessionRepository
from ktg_agent.operations.store import OperationPlanStore

MAX_BODY_BYTES = 64 * 1024


class PublicError(Exception):
    """Only explicitly safe, caller-facing messages belong here."""
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status, self.code, self.message = status, code, message


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}},
                        status_code=status, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


class RequestBoundary:
    """Bound POST bodies before parsing, including clients without Content-Length."""
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                if not any(k.lower() == b"cache-control" for k, _ in headers):
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
                headers = list(message["headers"])
                if not any(k.lower() == b"x-content-type-options" for k, _ in headers):
                    headers.append((b"x-content-type-options", b"nosniff"))
                message["headers"] = headers
            await send(message)

        if scope["method"] != "POST":
            await self.app(scope, receive, send_headers)
            return
        lengths = [v for k, v in scope["headers"] if k.lower() == b"content-length"]
        if lengths and (len(lengths) != 1 or not lengths[0].isdigit()
                        or not 0 < int(lengths[0]) <= MAX_BODY_BYTES):
            await error_response(400, "INVALID_REQUEST", "Invalid request body size.")(scope, receive, send_headers)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > MAX_BODY_BYTES:
                await error_response(400, "INVALID_REQUEST", "Invalid request body size.")(scope, receive, send_headers)
                return
            if not message.get("more_body", False):
                break
        if not body:
            await error_response(400, "INVALID_REQUEST", "Request body is required.")(scope, receive, send_headers)
            return
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send_headers)


@dataclass
class Resources:
    repository: SessionRepository
    operations: OperationPlanStore


type ResourceFactory = Callable[[Settings, AsyncExitStack], object]


def open_resources(settings: Settings, stack: AsyncExitStack) -> Resources:
    # These stores use per-operation context-managed connections, no persistent close.
    repository = create_session_repository(settings)
    operations = OperationPlanStore(repository)
    repository.recover_interrupted_runs()
    operations.recover_interrupted()
    return Resources(repository, operations)


def get_resources(request: Request) -> object:
    if not request.app.state.ready:
        raise PublicError(503, "NOT_READY", "Service is not ready.")
    return cast(object, request.app.state.resources)


class Health(BaseModel):
    status: Literal["ok"] = "ok"


def create_app(settings: Settings | None = None, *,
               resource_factory: ResourceFactory = open_resources,
               run_core: Core | None = None, run_events: Events | None = None) -> FastAPI:
    from ktg_agent.http_sse import RunEvents
    event_sink = run_events if run_events is not None else RunEvents()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.ready = False
        try:
            async with AsyncExitStack() as stack:
                configuration = settings if settings is not None else Settings.from_env()
                if not configuration.session_db_path or not Path(configuration.session_db_path).is_absolute():
                    raise RuntimeError("AGENT_SESSION_DB must be an explicit absolute isolated path")
                app.state.settings = configuration
                app.state.resources = resource_factory(configuration, stack)
                if isinstance(event_sink, RunEvents):
                    stack.callback(event_sink.close)
                if run_core is not None:
                    app.state.runs = RunRuntime(app.state.resources.repository, run_core, event_sink)
                    stack.push_async_callback(app.state.runs.close)
                app.state.ready = True
                try:
                    yield
                finally:
                    app.state.ready = False
        finally:
            app.state.resources = None

    app = FastAPI(lifespan=lifespan)
    app.state.ready = False
    app.state.run_events = event_sink
    app.state.resources = None
    app.add_middleware(RequestBoundary)

    @app.exception_handler(PublicError)
    async def public_error(request: Request, exc: PublicError) -> JSONResponse:
        return error_response(exc.status, exc.code, exc.message)

    @app.exception_handler(KTGError)
    async def ktg_error(request: Request, exc: KTGError) -> JSONResponse:
        # Do not copy raw upstream messages, URLs or credentials into responses.
        known = {
            "AUTH_REQUIRED": (401, "Authentication required."),
            "AUTH_INVALID": (401, "Invalid authentication."),
            "AUTH_EXPIRED": (401, "Authentication expired."),
            "PERMISSION_DENIED": (403, "Permission denied."),
            "PERMISSION_UNAVAILABLE": (503, "Permission service unavailable."),
            "OPERATIONS_DISABLED": (403, "Operations disabled."),
        }
        status, message = known.get(exc.code, (502, "MES service failed."))
        code = exc.code if exc.code in known else "MES_ERROR"
        return error_response(status, code, message)

    @app.exception_handler(ProviderError)
    async def provider_error(request: Request, exc: ProviderError) -> JSONResponse:
        return error_response(502, "PROVIDER_ERROR", "Model service failed.")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        invalid_json = any(e["type"] == "json_invalid" for e in exc.errors())
        return error_response(400, "INVALID_JSON" if invalid_json else "VALIDATION_ERROR",
                              "Invalid JSON." if invalid_json else "Invalid request.")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        if exc.status_code == 404:
            # HTTP-01 records this legacy shape; keep it pending consumer confirmation.
            return JSONResponse({"error": "NOT_FOUND"}, status_code=404,
                                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        response = error_response(exc.status_code, "HTTP_ERROR", "Request failed.")
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception) -> JSONResponse:
        return error_response(500, "INTERNAL_ERROR", "Internal server error.")

    @app.get("/health", response_model=Health)
    async def health(request: Request) -> Health:
        get_resources(request)
        return Health()

    from ktg_agent.http_rest import router
    app.include_router(router)
    from ktg_agent.http_run_rest import router as run_router
    app.include_router(run_router)
    from ktg_agent.http_sse import router as sse_router
    app.include_router(sse_router)
    return app

