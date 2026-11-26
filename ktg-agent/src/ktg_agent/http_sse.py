"""Single-loop, active-Run display cache. SQLite remains authoritative."""
from __future__ import annotations
import asyncio
from collections import deque, OrderedDict
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import cast, Annotated
from fastapi import APIRouter, Request, Response, Depends
from fastapi.sse import EventSourceResponse, ServerSentEvent
from ktg_agent.core.messages import JsonObject
from ktg_agent.http_app import PublicError, Resources
from ktg_agent.http_run_rest import Auth, Res

@dataclass
class Buffer:
    items: deque[JsonObject] = field(default_factory=deque)
    seq: int = 0
    closed: bool = False
    changed: asyncio.Event = field(default_factory=asyncio.Event)

class RunEvents:
    # 512 display frames per active Run; no historical Run retention.
    def __init__(self, capacity: int = 512) -> None:
        if capacity < 1:
            raise ValueError('capacity must be positive')
        self.capacity = capacity
        self.states: dict[str, Buffer] = {}
        self.subscribers = 0
        self.released: OrderedDict[str, int] = OrderedDict()

    def publish(self, run_id: str, kind: str, payload: JsonObject) -> None:
        import copy
        state = self.states.setdefault(run_id, Buffer())
        state.seq += 1
        state.items.append({'seq': state.seq, 'type': kind, 'payload': copy.deepcopy(payload),
                            'timestamp': datetime.now(timezone.utc).isoformat()})
        while len(state.items) > self.capacity:
            state.items.popleft()
        state.changed.set()
        if kind == 'run_settled':
            state.closed = True
            self.states.pop(run_id, None)
            # Tiny bounded tombstones close the DB-read/notification race.
            # No text or user data is retained after settlement.
            self.released[run_id] = state.seq
            while len(self.released) > 256:
                self.released.popitem(last=False)

    def close(self) -> None:
        for state in self.states.values():
            state.closed = True
            state.changed.set()
        self.states.clear()
        self.released.clear()

    def last_seq(self, run_id: str) -> int:
        state = self.states.get(run_id)
        return state.seq if state else self.released.get(run_id, 0)

    def expired(self, state: Buffer, seq: int) -> bool:
        return bool(state.items and seq < cast(int, state.items[0]['seq']) - 1)

    def read(self, state: Buffer, seq: int) -> list[JsonObject]:
        return [item for item in state.items if cast(int, item['seq']) > seq]

router = APIRouter(prefix='/agent-api/v1')

async def subscription(rid: str, request: Request, response: Response,
                 auth: Auth, resources: Res) -> tuple[RunEvents, Buffer, int, str]:
    # FastAPI executes through this validation before it starts the stream.
    params = list(request.query_params.multi_items())
    if (len(params) > 1 or any(k != 'after_seq' for k, _ in params)
            or (params and (not params[0][1].isascii() or not params[0][1].isdigit()
                            or len(params[0][1]) > 18))):
        raise PublicError(400, 'VALIDATION_ERROR', 'Invalid cursor.')
    seq = int(params[0][1]) if params else 0
    repo = cast(Resources, resources).repository
    run = await asyncio.to_thread(repo.get_run, auth.owner, rid)
    if run is None:
        raise PublicError(404, 'RUN_NOT_FOUND', 'Resource not found.')
    sink = cast(RunEvents, request.app.state.run_events)
    state = sink.states.get(rid)
    if state is None:
        if rid in sink.released or run.status not in ('queued', 'running', 'cancelling') or seq:
            raise PublicError(410, 'EVENT_HISTORY_EXPIRED', 'Read persisted Run state.')
        state = sink.states.setdefault(rid, Buffer())
    if seq > state.seq:
        raise PublicError(400, 'VALIDATION_ERROR', 'Invalid cursor.')
    if sink.expired(state, seq):
        raise PublicError(410, 'EVENT_HISTORY_EXPIRED', 'Read persisted Run state.')
    response.headers['Cache-Control'] = 'no-cache, no-store, no-transform'
    return sink, state, seq, run.session_id

@router.get('/runs/{rid}/events', response_class=EventSourceResponse)
async def events(rid: str, subscription: Annotated[tuple[RunEvents, Buffer, int, str], Depends(subscription)]) -> AsyncGenerator[ServerSentEvent, None]:
    sink, state, seq, session_id = subscription
    sink.subscribers += 1
    try:
        while True:
            if sink.expired(state, seq):
                # An open stream cannot change its HTTP status. No business ID:
                # client must read state and explicitly accept the display gap.
                yield ServerSentEvent(event='history_expired', data={
                    'run_id': rid, 'session_id': session_id,
                    'type': 'history_expired', 'resume_seq': state.seq})
                return
            batch = sink.read(state, seq)
            for item in batch:
                seq = cast(int, item['seq'])
                envelope = {**item, 'schema_version': 1, 'run_id': rid,
                            'session_id': session_id}
                yield ServerSentEvent(id=str(seq), event=cast(str,item['type']), data=envelope)
            if state.seq > seq:
                continue
            if state.closed:
                return
            # No await between reading and clearing: publish cannot race this
            # on the one asyncio loop. Each reader owns its own cursor.
            state.changed.clear()
            await state.changed.wait()
    finally:
        sink.subscribers -= 1
