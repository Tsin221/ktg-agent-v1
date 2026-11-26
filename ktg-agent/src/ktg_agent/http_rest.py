"""Authenticated REST adapters; business writes remain in OperationExecutor."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, cast
from collections.abc import Awaitable
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, field_validator

from ktg_agent.core.messages import JsonObject
from ktg_agent.http_app import PublicError, Resources, get_resources
from ktg_agent.ktg.client import KTGClient
from ktg_agent.tools.mes_catalog import available_tools
from ktg_agent.sessions.service import SessionService
from ktg_agent.sessions.task_tools import owner_from_identity
from ktg_agent.sessions.service import _safe_public
from ktg_agent.tools.native import MesToolDeps
from pydantic_ai.messages import ToolReturnPart, ModelRequest

router = APIRouter(prefix="/agent-api/v1")
bearer = HTTPBearer(auto_error=False)


def get_mes_client(request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> KTGClient:
    if credentials is None:
        raise PublicError(401, "AUTH_REQUIRED", "Authentication required.")
    settings = request.app.state.settings
    return KTGClient(base_url=settings.ktg_base_url, authorization=f"Bearer {credentials.credentials}",
                     scope_by_token=settings.scope_by_token)


@dataclass(repr=False)
class Authentication:
    client: KTGClient = field(repr=False)
    identity: JsonObject = field(repr=False)
    owner: str


async def authenticate(client: Annotated[KTGClient, Depends(get_mes_client)]) -> Authentication:
    identity = await client.get_current_user()
    return Authentication(client, identity, owner_from_identity(identity))


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Create(Body):
    title: str | None = None

    @field_validator("title")
    @classmethod
    def nonempty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("empty title")
        return value


class Rename(Create):
    title: str


class Digest(Body):
    digest: str

    @field_validator("digest")
    @classmethod
    def valid_digest(cls, value: str) -> str:
        import re
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            raise ValueError("invalid digest")
        return value


class SessionResponse(BaseModel):
    session_id: str
    title: str | None
    created_at: str
    updated_at: str
    active_run_id: str | None = None


class PageResponse(BaseModel):
    items: list[dict[str, Any]]
    next_cursor: int | None = None


class StatsResponse(BaseModel):
    session_id: str
    message_count: int
    messages_by_role: dict[str, int]
    run_count: int
    runs_by_status: dict[str, int]
    known_usage: dict[str, Any]
    context: dict[str, Any]


class ItemsResponse(BaseModel):
    items: list[dict[str, Any]]


class RequestServices:
    def __init__(self, request: Request, resources: Resources, auth: Authentication) -> None:
        # The identity snapshot is request-local; permission reads and MES writes use
        # the original client and are never cached across requests.
        class IdentityClient:
            async def get_current_user(self) -> JsonObject:
                return auth.identity

            def __getattr__(self, name: str) -> Any:
                return getattr(auth.client, name)

        client = cast(KTGClient, IdentityClient())
        self.authorization = ""  # client already holds the credential privately
        self.auth = auth
        self.operations = resources.operations
        self.sessions = SessionService(resources.repository, lambda _: client,
            operation_store=resources.operations, operation_enabled=lambda: True)


def services(request: Request, resources: Annotated[object, Depends(get_resources)],
             auth: Annotated[Authentication, Depends(authenticate)]) -> RequestServices:
    return RequestServices(request, cast(Resources, resources), auth)


Service = Annotated[RequestServices, Depends(services)]


def pagination(request: Request, *, allowed: bool = True) -> tuple[int | None, int]:
    pairs = list(request.query_params.multi_items())
    if (not allowed and pairs) or len({k for k, _ in pairs}) != len(pairs):
        raise PublicError(400, "VALIDATION_ERROR", "Invalid query parameters.")
    values: dict[str, int] = {}
    for key, value in pairs:
        if key not in {"cursor", "limit"} or not value.isascii() or not value.isdecimal() or int(value) <= 0:
            raise PublicError(400, "VALIDATION_ERROR", "Invalid query parameters.")
        values[key] = int(value)
    return values.get("cursor"), min(values.get("limit", 50), 100)


async def invoke(call: Awaitable[JsonObject]) -> JsonObject:
    # Legacy application services signal resource absence and rejected actions this way.
    try:
        return await call
    except KeyError:
        raise PublicError(404, "SESSION_NOT_FOUND", "Resource not found.") from None
    except ValueError:
        raise PublicError(400, "VALIDATION_ERROR", "Invalid request or operation state.") from None


@router.get("/tools", response_model=dict[str, Any])
async def tools(request: Request, svc: Service) -> JsonObject:
    pagination(request, allowed=False)
    return await available_tools(svc.auth.client, svc.auth.identity, svc.operations)


@router.post("/sessions", status_code=201, response_model=SessionResponse, response_model_exclude_unset=True)
async def create(request: Request, body: Create, svc: Service) -> JsonObject:
    pagination(request, allowed=False)
    return await invoke(svc.sessions.create(svc.authorization, body.title))


@router.get("/sessions", response_model=PageResponse)
async def sessions(request: Request, svc: Service) -> JsonObject:
    cursor, limit = pagination(request)
    return await invoke(svc.sessions.list(svc.authorization, cursor=cursor, limit=limit))


@router.get("/sessions/{sid}", response_model=SessionResponse)
async def session(sid: str, request: Request, svc: Service) -> JsonObject:
    pagination(request)
    return await invoke(svc.sessions.get(svc.authorization, sid))


@router.post("/sessions/{sid}/title", response_model=SessionResponse, response_model_exclude_unset=True)
async def rename(sid: str, body: Rename, request: Request, svc: Service) -> JsonObject:
    pagination(request, allowed=False)
    return await invoke(svc.sessions.rename(svc.authorization, sid, body.title))


@router.get("/sessions/{sid}/messages", response_model=PageResponse)
async def messages(sid: str, request: Request, svc: Service) -> JsonObject:
    cursor, limit = pagination(request)
    return await invoke(svc.sessions.messages(svc.authorization, sid, cursor=cursor, limit=limit))


@router.get("/sessions/{sid}/stats", response_model=StatsResponse)
async def stats(sid: str, request: Request, svc: Service) -> JsonObject:
    pagination(request, allowed=False)
    result = await invoke(svc.sessions.stats(svc.authorization, sid))
    context = cast(JsonObject, result['context'])
    for key in ('summary_id', 'covers_start_message_id', 'covers_end_message_id', 'summary_token_estimate'):
        context.pop(key, None)
    return result


@router.get("/sessions/{sid}/tasks", response_model=ItemsResponse)
@router.get("/sessions/{sid}/tasks/{task_id}", response_model=dict[str, Any])
async def tasks(sid: str, request: Request, svc: Service, task_id: str | None = None) -> JsonObject:
    pagination(request, allowed=False)
    await invoke(svc.sessions.get(svc.authorization, sid))
    return await invoke(svc.sessions.tasks(svc.authorization, sid, task_id))


@router.get("/sessions/{sid}/operations", response_model=ItemsResponse)
@router.get("/sessions/{sid}/operations/{plan_id}", response_model=dict[str, Any])
async def operations(sid: str, request: Request, svc: Service, plan_id: str | None = None) -> JsonObject:
    pagination(request, allowed=False)
    return await invoke(svc.sessions.operation_plans(svc.authorization, sid, plan_id))


@router.get("/operations/history", response_model=ItemsResponse)
async def historical_operations(request: Request, svc: Service) -> JsonObject:
    pagination(request, allowed=False)
    return await invoke(svc.sessions.historical_operations(svc.authorization))


@router.post("/sessions/{sid}/operations/{plan_id}/confirm", response_model=dict[str, Any])
async def confirm(sid: str, plan_id: str, body: Digest, request: Request, svc: Service) -> JsonObject:
    pagination(request, allowed=False)
    return await invoke(svc.sessions.operation_action(svc.authorization, sid, plan_id, "confirm", {"digest": body.digest}))


@router.post("/sessions/{sid}/operations/{plan_id}/cancel", response_model=dict[str, Any])
async def cancel(sid: str, plan_id: str, body: Digest, request: Request, svc: Service) -> JsonObject:
    pagination(request, allowed=False)
    return await invoke(svc.sessions.operation_action(svc.authorization, sid, plan_id, "cancel", {"digest": body.digest}))


@router.post("/sessions/{sid}/operations/{plan_id}/reconcile", response_model=dict[str, Any])
async def reconcile(sid: str, plan_id: str, body: Body, request: Request, svc: Service) -> JsonObject:
    pagination(request, allowed=False)
    return await invoke(svc.sessions.operation_action(svc.authorization, sid, plan_id, "reconcile", {}))

class Query(Body):
    message: str

    @field_validator("message")
    @classmethod
    def valid_message(cls, value: str) -> str:
        if not value.strip() or len(value.strip()) > 2000:
            raise ValueError("invalid message")
        return value.strip()


@router.post("/query", response_model=dict[str, Any])
async def query(request: Request, body: Query, auth: Annotated[Authentication, Depends(authenticate)]) -> JsonObject:
    pagination(request, allowed=False)
    from ktg_agent.http_app import usage_limits
    agent = getattr(request.app.state, 'query_agent', None)
    if agent is None:
        raise PublicError(503, 'RUN_UNAVAILABLE', 'Run core is unavailable.')
    run_id = str(uuid4())
    result = await agent.run(body.message, deps=MesToolDeps(run_id, run_id, auth.owner, auth.identity, auth.client),
                             run_id=run_id, usage_limits=usage_limits(request.app.state.settings), timeout=90)
    results = []
    for message in result.all_messages():
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, ToolReturnPart) and isinstance(part.metadata, dict):
                    details = _safe_public(part.metadata.get('details'))
                    if isinstance(details, dict):
                        results.append(details)
    return {'run_id': run_id, 'answer': result.output, 'stop_reason': 'completed',
            'current_user': str(auth.identity.get('userName', '')), 'results': results}


