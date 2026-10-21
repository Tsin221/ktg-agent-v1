"""Temporary long-session probe. Prints only timing, counts, usage and anonymized run IDs."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from time import monotonic
from typing import cast

from ktg_agent.context.tokens import estimate_messages_tokens
from ktg_agent.context.window import ContextPolicy
from ktg_agent.core.events import EventPublisher, EventSink
from ktg_agent.core.loop import AgentRunResult
from ktg_agent.core.messages import JsonObject, Message, TextBlock, UserMessage
from ktg_agent.ktg import KTGClient
from ktg_agent.providers.base import ModelConfig
from ktg_agent.providers.openai_compatible import OpenAICompatibleProvider
from ktg_agent.service import QueryService
from ktg_agent.sessions.coordinator import RegisterAgent, RunCoordinator
from ktg_agent.sessions.repository import SessionRepository
from ktg_agent.sessions.service import SessionService
from ktg_agent.context.compaction import PreparedCompaction


class FakeClient:
    async def get_current_user(self) -> dict[str, object]:
        return {"userId": 7, "userName": "probe"}


async def main() -> None:
    started = monotonic()
    provider = OpenAICompatibleProvider(
        api_key=os.environ["MODEL_API_KEY"], base_url=os.environ["MODEL_BASE_URL"])
    policy = ContextPolicy(4096, 1024)
    query = QueryService(
        provider=provider, model_config=ModelConfig(os.environ["MODEL_ID"], 512),
        ktg_client_factory=lambda _: cast(KTGClient, FakeClient()),
        context_policy=policy, summary_max_tokens=256,
    )
    with tempfile.TemporaryDirectory(prefix="t29-context-") as temporary:
        repo = SessionRepository(Path(temporary) / "sessions.sqlite3")
        session = repo.create_session("id:7")
        history = "历史记录，仅用于窗口测试。" * 100
        for index in range(12):
            run = repo.create_run("id:7", session.id, f"prior-{index}", f"hash-{index}", {})
            repo.settle_run("id:7", run.id, "completed", "completed", {},
                            (UserMessage((TextBlock(history),)),))
        history_tokens = estimate_messages_tokens(repo.completed_history("id:7", session.id))

        async def execute(run_id: str, message: str, prior: tuple[Message, ...],
                          page_context: JsonObject | None, authorization: str,
                          event_sink: EventSink, register_agent: RegisterAgent) -> AgentRunResult:
            return await query.run_session(
                run_id=run_id, message=message, history=prior,
                page_context=page_context, authorization=authorization,
                event_sink=event_sink, on_agent_ready=register_agent)

        async def compact(run_id: str, prepared: PreparedCompaction,
                          sink: EventSink, publisher: EventPublisher,
                          register_agent: RegisterAgent) -> AgentRunResult:
            return await query.run_compaction(run_id, prepared, event_sink=sink,
                                              publisher=publisher, on_agent_ready=register_agent)

        coordinator = RunCoordinator(
            repo, execute, query.budget_snapshot,
            compaction_planner=query.plan_compaction,
            compaction_executor=compact,
            system_prompt_builder=query.build_system_prompt,
            summary_budget_snapshot=query.summary_budget_snapshot,
        )
        service = SessionService(repo, lambda _: cast(KTGClient, FakeClient()), coordinator,
                                 context_policy=policy,
                                 system_prompt_builder=query.build_system_prompt,
                                 tool_schemas=query.model_tool_schemas)
        try:
            accepted = await service.create_run("Bearer probe", session.id, "real-long-1",
                                                "请只回复：好。")
            run_id = cast(str, accepted["run_id"])
            for _ in range(120):
                run = repo.get_run("id:7", run_id)
                assert run is not None
                if run.status not in {"queued", "running", "cancelling"}:
                    break
                await asyncio.sleep(0.5)
            assert run is not None
            stats = await service.stats("Bearer probe", session.id)
            context = cast(JsonObject, stats["context"])
            compact_runs = [item for item in repo.list_runs("id:7", session.id).items
                            if item.kind == "compaction"]
            print(f"elapsed_s={monotonic() - started:.3f} history_estimated_tokens={history_tokens} "
                  f"window_tokens={policy.window_tokens} threshold_tokens={context['threshold_tokens']}")
            print(f"user_run={run_id[:8]} status={run.status} "
                  f"context_estimated_tokens={run.context.get('estimated_tokens') if run.context else None} "
                  f"retained_message_count={run.context.get('retained_message_count') if run.context else None}")
            for item in compact_runs:
                print(f"compaction_run={item.id[:8]} status={item.status} "
                      f"error_code={item.result_summary.get('error_code') if item.result_summary else None}")
            usage = cast(JsonObject, stats["known_usage"])
            print(f"known_input_tokens={usage['known_input_tokens']} "
                  f"known_output_tokens={usage['known_output_tokens']} "
                  f"known_total_tokens={usage['known_total_tokens']} "
                  f"unknown_request_count={usage['unknown_request_count']}")
            if run.status != "completed":
                raise RuntimeError("real long-session run did not complete")
        finally:
            coordinator.close()


if __name__ == "__main__":
    asyncio.run(main())
