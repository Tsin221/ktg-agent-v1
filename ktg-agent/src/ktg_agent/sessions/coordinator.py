"""Process-owned workers for admitted session runs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor

from ktg_agent.core.messages import JsonObject
from ktg_agent.sessions.repository import RunRecord, SessionRepository

RunExecutor = Callable[[str, str], Awaitable[JsonObject]]


class RunCoordinator:
    """Owns run tasks beyond the lifetime of each HTTP request event loop."""

    def __init__(self, repository: SessionRepository, executor: RunExecutor) -> None:
        self._repository = repository
        self._executor = executor
        self._workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent-run")

    def schedule(self, run: RunRecord, message: str, authorization: str) -> None:
        try:
            self._workers.submit(self._execute, run, message, authorization)
        except RuntimeError:
            self._repository.update_run_status(
                run.owner_user_id, run.id, "failed", stop_reason="SCHEDULING_FAILED",
            )
            raise

    def _execute(self, run: RunRecord, message: str, authorization: str) -> None:
        try:
            self._repository.update_run_status(run.owner_user_id, run.id, "running")
            result = asyncio.run(self._invoke(message, authorization))
            stop_reason = result.get("stop_reason")
            status = ("completed" if stop_reason == "completed" else
                      "cancelled" if stop_reason == "cancelled" else "failed")
            self._repository.update_run_status(
                run.owner_user_id, run.id, status,
                stop_reason=stop_reason if isinstance(stop_reason, str) else "EXECUTION_FAILED",
                result_summary={key: value for key, value in result.items() if key != "run_id"},
            )
        except Exception:
            # The request was admitted already; release the session without storing secrets.
            self._repository.update_run_status(
                run.owner_user_id, run.id, "failed", stop_reason="EXECUTION_FAILED",
            )

    async def _invoke(self, message: str, authorization: str) -> JsonObject:
        return await self._executor(message, authorization)

    def close(self) -> None:
        self._workers.shutdown(wait=True)
