"""Authenticated Run REST boundary; SSE belongs to HTTP-05."""
from typing import Annotated, Any, Literal, cast
import asyncio
from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field, field_validator
from ktg_agent.http_app import PublicError, Resources, get_resources
from ktg_agent.http_rest import Body, Authentication, authenticate, pagination
from ktg_agent.http_runs import RunRuntime, public_run_summary
from ktg_agent.core.messages import JsonObject
from ktg_agent.sessions.service import SessionService
from ktg_agent.sessions.repository import (RunConflictError, RunBusyError,
    ControlConflictError, ControlStateError)

router = APIRouter(prefix='/agent-api/v1')
class Control(Body):
    request_id: str = Field(min_length=1, max_length=128)
    @field_validator('request_id')
    @classmethod
    def identifier(cls, value: str) -> str:
        if not value.strip():
            raise ValueError('empty request_id')
        return value.strip()
class Input(Control):
    message: str = Field(min_length=1, max_length=2000)
    @field_validator('message')
    @classmethod
    def text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError('empty message')
        return value.strip()
class CreateRun(Input):
    page_context: dict[str, Any] | None = None
    task_id: str | None = Field(default=None, min_length=1, max_length=128)
class RunInput(Input):
    mode: Literal['steer','follow_up']

Auth = Annotated[Authentication, Depends(authenticate)]
Res = Annotated[object, Depends(get_resources)]

def runtime(request: Request) -> RunRuntime:
    value = getattr(request.app.state, 'runs', None)
    if value is None or value.closing:
        raise PublicError(503,'RUN_UNAVAILABLE','Run core is unavailable.')
    return cast(RunRuntime, value)

async def invoke(call: Any) -> Any:
    try:
        return await call
    except (RunConflictError, ControlConflictError):
        raise PublicError(409,'REQUEST_ID_CONFLICT','Request ID conflicts.') from None
    except RunBusyError:
        raise PublicError(409,'RUN_BUSY','Session has an active run.') from None
    except ControlStateError as exc:
        raise PublicError(409,exc.code,'Run does not accept this control.') from None
    except KeyError:
        raise PublicError(404,'RUN_NOT_FOUND','Resource not found.') from None
    except ValueError:
        raise PublicError(400,'VALIDATION_ERROR','Invalid request.') from None

@router.post('/sessions/{sid}/runs', status_code=202)
async def create(sid: str, body: CreateRun, request: Request, auth: Auth, resources: Res) -> Any:
    pagination(request, allowed=False)
    repo = cast(Resources,resources).repository
    if await asyncio.to_thread(repo.get_session,auth.owner,sid) is None:
        raise PublicError(404,'SESSION_NOT_FOUND','Resource not found.')
    try:
        context = SessionService._page_context(body.page_context)
    except ValueError:
        raise PublicError(400,'VALIDATION_ERROR','Invalid page context.') from None
    existing = await asyncio.to_thread(repo.find_run_by_request, auth.owner, sid, body.request_id)
    if body.task_id is not None and existing is None:
        # Validate against the current native plan only for a new admission.
        task = await invoke(asyncio.to_thread(repo.get_task,auth.owner,sid,body.task_id))
        versions = task.get('versions', [])
        steps = versions[0].get('steps', []) if versions else []
        if steps and all(step.get('status') == 'completed' for step in steps):
            raise PublicError(409,'TASK_NOT_ACTIVE','Task is already complete.')
    payload: JsonObject = {'message':body.message,'page_context':context}
    if body.task_id is not None:
        payload['task_id'] = body.task_id
    run = await invoke(runtime(request).create(auth.owner,sid,body.request_id,payload,auth))
    return {'run_id':run.id,'status':run.status}

@router.get('/runs/{rid}')
async def get(rid: str, request: Request, auth: Auth, resources: Res) -> Any:
    pagination(request, allowed=False)
    repo = cast(Resources, resources).repository
    run = await asyncio.to_thread(repo.get_run,auth.owner,rid)
    if run is None:
        raise PublicError(404,'RUN_NOT_FOUND','Resource not found.')
    rt = getattr(request.app.state,'runs',None)
    return {'run_id':rid,'session_id':run.session_id,'status':run.status,
        'stop_reason':run.stop_reason,'result_summary':await public_run_summary(repo, auth.owner, run),'usage':run.usage,
        'last_seq':rt.events.last_seq(rid) if rt and rt.events else 0}

@router.post('/runs/{rid}/cancel',status_code=202)
async def cancel(rid: str, body: Control, request: Request, response: Response, auth: Auth) -> Any:
    pagination(request, allowed=False)
    result = await invoke(runtime(request).control(auth.owner,rid,body.request_id,'cancel',{}))
    if result['cancel_status'] != 'accepted':
        response.status_code = 200
    return result

@router.post('/runs/{rid}/inputs',status_code=202)
async def add_input(rid: str, body: RunInput, request: Request, auth: Auth) -> Any:
    pagination(request, allowed=False)
    return await invoke(runtime(request).control(auth.owner,rid,body.request_id,body.mode,{'message':body.message}))

@router.get('/runs/{rid}/inputs')
async def inputs(rid: str, request: Request, auth: Auth, resources: Res) -> Any:
    cursor,limit = pagination(request)
    repo = cast(Resources,resources).repository
    if await asyncio.to_thread(repo.get_run,auth.owner,rid) is None:
        raise PublicError(404,'RUN_NOT_FOUND','Resource not found.')
    page = await asyncio.to_thread(repo.list_inputs,auth.owner,rid,after=cursor,limit=limit)
    return {'items':[{'request_id':i.request_id,'mode':i.kind,'message':i.request['message'],
        'status':i.status,'reason':i.reason,'created_at':i.created_at,'updated_at':i.updated_at}
        for i in page.items], 'next_cursor':page.next_cursor}
