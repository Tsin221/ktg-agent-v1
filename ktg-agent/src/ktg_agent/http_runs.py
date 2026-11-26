"""Application-owned asyncio runs; core adapters must report verified outcomes.

No model implementation or streaming event sequence allocator lives here.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from pydantic_ai.messages import ModelMessage

from ktg_agent.core.messages import JsonObject, Message
from ktg_agent.sessions.repository import SessionRepository, RunRecord

log = logging.getLogger(__name__)


@dataclass
class Outcome:
    status: str
    reason: str
    summary: JsonObject = field(default_factory=dict)
    messages: tuple[Message, ...] = ()
    native_messages: tuple[ModelMessage, ...] | None = None


@runtime_checkable
class NativeSettlementRepository(Protocol):
    """Application transaction boundary for native history plus terminal state.

    The legacy repository deliberately does not implement this until the
    unified SQLite schema is designed. Never project native history to legacy
    Message just to allow settlement.
    """
    def settle_native_run(self, owner_user_id: str, run_id: str, status: str,
                          stop_reason: str, result_summary: JsonObject,
                          messages: tuple[ModelMessage, ...]) -> RunRecord: ...


class Execution(Protocol):
    async def run(self) -> Outcome: ...
    def stop(self) -> None:
        """Nonblocking cooperative stop; prevent new work, preserve unknown receipts."""
        ...
    async def input(self, request_id: str, mode: str, message: str) -> bool:
        """True only when consumption is confirmed; False leaves input queued."""
        ...


class Core(Protocol):
    def prepare(self, run: RunRecord, credential: object) -> Execution: ...


class Events(Protocol):
    def publish(self, run_id: str, kind: str, payload: JsonObject) -> None: ...
    def last_seq(self, run_id: str) -> int: ...


class RunRuntime:
    def __init__(self, repository: SessionRepository, core: Core,
                 events: Events | None = None, *, shutdown_timeout: float = 2) -> None:
        self.repository, self.core, self.events = repository, core, events
        self.shutdown_timeout = shutdown_timeout
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.executions: dict[str, Execution] = {}
        self.lock = asyncio.Lock()
        self.closing = False
        self.admissions: set[asyncio.Task[RunRecord]] = set()
        self.unresolved: set[str] = set()

    async def create(self, owner: str, session_id: str, request_id: str,
                     payload: JsonObject, credential: object) -> RunRecord:
        # Admission outlives a disconnected HTTP request, including SQLite I/O.
        task = asyncio.create_task(self._create(owner, session_id, request_id, payload, credential))
        self.admissions.add(task)
        def observed(done: asyncio.Task[RunRecord]) -> None:
            if not done.cancelled():
                done.exception()
            self.admissions.discard(done)
        task.add_done_callback(observed)
        return await asyncio.shield(task)

    async def _create(self, owner: str, session_id: str, request_id: str,
                      payload: JsonObject, credential: object) -> RunRecord:
        async with self.lock:
            if self.closing:
                raise RuntimeError('runtime closing')
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True,
                ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
            run, created = await asyncio.to_thread(self.repository.admit_run,
                owner, session_id, request_id, digest, payload)
            if not created:
                return run
            try:
                execution = self.core.prepare(run, credential)
                self.executions[run.id] = execution
                coroutine = self._execute(owner, run, execution)
                try:
                    task = asyncio.create_task(coroutine, name=f'run:{run.id}')
                except BaseException:
                    coroutine.close()
                    raise
                self.tasks[run.id] = task
                task.add_done_callback(lambda t: self._done(run.id, t))
            except Exception:
                self.executions.pop(run.id, None)
                await self._settle(owner, run.id, Outcome('failed', 'schedule_failed'))
                raise
            return run

    def _done(self, run_id: str, task: asyncio.Task[None]) -> None:
        if task.cancelled() or task.exception() is not None:
            self.unresolved.add(run_id)
            log.error('Run requires recovery: %s', run_id)
        self.tasks.pop(run_id, None)
        self.executions.pop(run_id, None)

    async def _settle(self, owner: str, run_id: str, outcome: Outcome) -> None:
        try:
            if outcome.native_messages is not None:
                if not isinstance(self.repository, NativeSettlementRepository):
                    raise RuntimeError('native SQLite terminal settlement is not connected')
                record = await asyncio.to_thread(self.repository.settle_native_run, owner, run_id,
                    outcome.status, outcome.reason, outcome.summary, outcome.native_messages)
            else:
                record = await asyncio.to_thread(self.repository.settle_run, owner, run_id,
                    outcome.status, outcome.reason, outcome.summary, outcome.messages)
        except Exception:
            self.unresolved.add(run_id)
            log.error('Run settlement failed; recovery required: %s', run_id)
            raise
        if self.events is not None:
            try:
                self.events.publish(run_id, 'run_settled', {'status': record.status,
                    'stop_reason': record.stop_reason, 'result_summary': record.result_summary})
            except Exception:
                log.error('Run notification failed: %s', run_id)

    async def _execute(self, owner: str, run: RunRecord, execution: Execution) -> None:
        async with self.lock:
            current = await asyncio.to_thread(self.repository.get_run, owner, run.id)
            assert current is not None
            if current.status == 'cancelling':
                await self._settle(owner, run.id, Outcome('cancelled', 'cancelled'))
                return
            await asyncio.to_thread(self.repository.update_run_status, owner, run.id, 'running')
        try:
            outcome = await execution.run()
        except asyncio.CancelledError:
            # External cancellation cannot prove that an outstanding write stopped.
            outcome = Outcome('needs_reconciliation', 'execution_interrupted')
        except Exception:
            outcome = Outcome('needs_reconciliation', 'execution_failed')
        async with self.lock:
            await self._settle(owner, run.id, outcome)

    async def control(self, owner: str, run_id: str, request_id: str,
                      kind: str, payload: JsonObject) -> JsonObject:
        async with self.lock:
            execution = self.executions.get(run_id)
            record, created = await asyncio.to_thread(self.repository.admit_control,
                owner, run_id, request_id, kind, payload, accepting=not self.closing,
                ready=execution is not None, closed=execution is None)
            if created and record.reason is None and execution is not None:
                if kind == 'cancel':
                    execution.stop()
                elif await self._deliver(execution, request_id, kind, str(payload['message'])):
                    await asyncio.to_thread(self.repository.mark_input_consumed,
                        owner, run_id, request_id)
                    return {'run_id': run_id, 'request_id': request_id, 'mode': kind,
                            'input_status': 'consumed'}
            current = await asyncio.to_thread(self.repository.get_run, owner, run_id)
            assert current is not None
            if kind == 'cancel':
                return {'run_id': run_id, 'request_id': request_id, 'status': current.status,
                        'cancel_status': record.reason or record.status}
            return {'run_id': run_id, 'request_id': request_id, 'mode': kind,
                    'input_status': record.status}

    async def _deliver(self, execution: Execution, request_id: str,
                       kind: str, message: str) -> bool:
        try:
            async with asyncio.timeout(1):
                return await execution.input(request_id, kind, message)
        except Exception:
            # Durable queued means unconfirmed, never a fabricated consumption.
            return False

    def publish_progress(self, run_id: str, kind: str, payload: JsonObject) -> None:
        """Core adapter may forward display events; HTTP-05 alone assigns IDs."""
        if kind == 'run_settled':
            raise ValueError('terminal notifications require a committed outcome')
        if run_id not in self.tasks:
            raise KeyError('active run not found')
        if self.events is not None:
            try:
                self.events.publish(run_id, kind, payload)
            except Exception:
                log.error('Run progress notification failed: %s', run_id)

    async def close(self) -> None:
        async with self.lock:
            self.closing = True
            for run_id, execution in tuple(self.executions.items()):
                try:
                    execution.stop()
                except Exception:
                    self.unresolved.add(run_id)
        if self.tasks:
            _, pending = await asyncio.wait(tuple(self.tasks.values()), timeout=self.shutdown_timeout)
            if pending:
                self.unresolved.update(k for k, v in self.tasks.items() if v in pending)
                log.error('Shutdown incomplete; active records retained for recovery')
