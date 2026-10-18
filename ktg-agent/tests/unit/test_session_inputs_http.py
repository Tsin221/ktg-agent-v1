"""HTTP contract for adding input to an active run."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from typing import cast

from ktg_agent.core.messages import AssistantMessage, StopReason, TextBlock, UserMessage
from ktg_agent.ktg import KTGClient
from ktg_agent.providers.base import FinalResponse, ModelConfig, ModelEvent, ProviderRequest, ResponseCompleted, Usage
from ktg_agent.service import QueryService
from ktg_agent.sessions.coordinator import RunCoordinator, RunPhase
from ktg_agent.sessions.repository import SessionRepository
from test_session_cancel_http import served
from test_session_http import FakeClient, request
from ktg_agent.core.events import AgentEvent, EventSink
from ktg_agent.core.loop import AgentRunResult
from ktg_agent.core.messages import JsonObject, Message
from ktg_agent.sessions.coordinator import RegisterAgent


class PausedProvider:
    def __init__(self) -> None:
        self.started = Event()
        self.release = asyncio.Event()
        self.owner_loop: asyncio.AbstractEventLoop | None = None
        self.requests: list[ProviderRequest] = []

    def stream(self, request: ProviderRequest) -> AsyncIterator[ModelEvent]:
        async def events() -> AsyncIterator[ModelEvent]:
            self.requests.append(request)
            if len(self.requests) == 1:
                self.owner_loop = asyncio.get_running_loop()
                self.started.set()
                await self.release.wait()
            yield ResponseCompleted(FinalResponse(
                AssistantMessage((TextBlock("answer"),)), StopReason.COMPLETED, Usage(1, 1),
            ))
        return events()


def test_input_http_concurrent_retry_pagination_and_history(tmp_path: Path) -> None:
    provider = PausedProvider()
    repo = SessionRepository(tmp_path / "sessions.sqlite3")
    session = repo.create_session("id:7")
    run, _ = repo.admit_run("id:7", session.id, "run-1", "hash-1", {})
    service = QueryService(
        provider=provider, model_config=ModelConfig("fake", 128),
        ktg_client_factory=lambda token: cast(KTGClient, FakeClient(token)),
    )
    agent_end_started = Event()
    release_agent_end = asyncio.Event()

    async def execute(run_id: str, message: str, history: tuple[Message, ...],
                      page_context: JsonObject | None, authorization: str,
                      sink: EventSink, register_agent: RegisterAgent) -> AgentRunResult:
        async def gated_sink(event: AgentEvent) -> None:
            if event.type.value == "agent_end":
                agent_end_started.set()
                await release_agent_end.wait()
            sink(event)

        return await service.run_session(
            run_id=run_id, message=message, history=history, page_context=page_context,
            authorization=authorization, event_sink=gated_sink, on_agent_ready=register_agent,
        )

    coordinator = RunCoordinator(repo, execute)
    path = f"/agent-api/v1/runs/{run.id}/inputs"
    with served(repo, coordinator) as server:
        body = {"request_id": "first", "mode": "follow_up", "message": " second "}
        assert request(server, "POST", path, body=body)[1]["error"]["code"] == "RUN_NOT_RUNNING"  # type: ignore[index]
        coordinator.schedule(run, "first", "Bearer alice")
        assert provider.started.wait(5)
        assert coordinator.phase(run.id) is RunPhase.RUNNING
        assert request(server, "POST", path, token="Bearer bob", body=body)[0] == 404
        assert request(server, "GET", path, token="Bearer bob")[0] == 404
        assert request(server, "GET", path, token="Bearer expired")[0] == 401
        for invalid in ({}, {**body, "extra": 1}, {**body, "mode": "other"},
                        {**body, "message": " "}, {**body, "request_id": ""}):
            assert request(server, "POST", path, body=invalid)[0] == 400

        barrier = Barrier(3)

        def submit() -> tuple[int, dict[str, object]]:
            barrier.wait()
            return request(server, "POST", path, body=body)

        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(submit)
            second = workers.submit(submit)
            barrier.wait()
            expected = (202, {"request_id": "first", "run_id": run.id,
                              "mode": "follow_up", "input_status": "queued"})
            assert first.result(5) == expected
            assert second.result(5) == expected
        assert request(server, "POST", path, body={**body, "message": "different"})[1]["error"]["code"] == "REQUEST_ID_CONFLICT"  # type: ignore[index]
        assert request(server, "POST", path, body={"request_id": "steer", "mode": "steer",
                                                     "message": "new direction"})[0] == 202
        assert request(server, "POST", path, body={"request_id": "second-follow-up",
                                                     "mode": "follow_up", "message": "third"})[0] == 202
        page = request(server, "GET", path + "?limit=1")
        assert page[0] == 200
        assert [item["request_id"] for item in cast(list[dict[str, object]], page[1]["items"])] == ["first"]
        cursor = page[1]["next_cursor"]
        assert request(server, "GET", path + f"?cursor={cursor}&limit=1")[1]["items"][0]["request_id"] == "steer"  # type: ignore[index]
        for query in ("?limit=0", "?cursor=-1", "?unknown=1", "?limit=1&limit=2"):
            assert request(server, "GET", path + query)[0] == 400
        assert provider.owner_loop is not None
        provider.owner_loop.call_soon_threadsafe(provider.release.set)
        assert agent_end_started.wait(5)
        assert request(server, "POST", path, body={"request_id": "closed", "mode": "steer",
                                                     "message": "too late"})[1]["error"]["code"] == "RUN_NOT_ACTIVE"  # type: ignore[index]
        assert all(item.type != "run_settled" for item in repo.list_events("id:7", run.id).items)
        provider.owner_loop.call_soon_threadsafe(release_agent_end.set)
        coordinator.close()
        assert repo.get_run("id:7", run.id).status == "completed"  # type: ignore[union-attr]
        assert len(provider.requests) == 3
        assert provider.requests[1].messages[-1] == UserMessage((TextBlock("new direction"),))
        assert provider.requests[2].messages[-2:] == (
            UserMessage((TextBlock("second"),)), UserMessage((TextBlock("third"),)),
        )
        assert [item.status for item in repo.list_inputs("id:7", run.id).items] == [
            "consumed", "consumed", "consumed"]
        assert request(server, "POST", path, body=body)[1]["input_status"] == "consumed"
        assert request(server, "POST", path, body={"request_id": "late", "mode": "steer",
                                                     "message": "late"})[1]["error"]["code"] == "RUN_NOT_ACTIVE"  # type: ignore[index]
        assert repo.list_events("id:7", run.id).items[-1].type == "run_settled"
        assert repo.completed_history("id:7", session.id).count(UserMessage((TextBlock("second"),))) == 1
