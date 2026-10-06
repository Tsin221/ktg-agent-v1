"""Application service for one authenticated, read-only Agent query."""

from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

from ktg_agent.core.loop import AgentLoop, RunLimits
from ktg_agent.core.messages import (
    JsonObject,
    JsonValue,
    SystemMessage,
    TextBlock,
    ToolResultMessage,
    UserMessage,
)
from ktg_agent.ktg import KTGClient, build_query_tools
from ktg_agent.providers.base import ModelConfig, Provider
from ktg_agent.tools import ToolRegistry, ToolRunner


type KTGClientFactory = Callable[[str], KTGClient]


SYSTEM_PROMPT = """你是 KTG MES 的只读查询助手。
必须通过已注册工具查询事实，不得猜测产品、库存或工单数据。
所有查询都使用当前网页登录用户的身份和权限；权限不足时如实说明，不能改用其他身份。
只允许查询产品资料、分仓库存和生产工单。不要请求、建议或声称执行任何新增、修改、删除操作。
库存结果要说明仓库、数量和单位；分页结果必须说明当前返回条数与总数，不能把当前页合计称为全部库存。
回答使用简洁中文。"""


class QueryService:
    def __init__(
        self,
        *,
        provider: Provider,
        model_config: ModelConfig,
        ktg_client_factory: KTGClientFactory,
    ) -> None:
        self._provider = provider
        self._model_config = model_config
        self._ktg_client_factory = ktg_client_factory

    async def query(self, *, message: str, authorization: str) -> JsonObject:
        clean_message = message.strip()
        if not clean_message:
            raise ValueError("查询内容不能为空。")
        if len(clean_message) > 2000:
            raise ValueError("查询内容不能超过 2000 个字符。")

        client = self._ktg_client_factory(authorization)
        identity = await client.get_current_user()
        definitions = build_query_tools()
        registry = ToolRegistry(definitions)
        loop = AgentLoop(
            provider=self._provider,
            tool_registry=registry,
            tool_runner=ToolRunner(registry),
            model_config=self._model_config,
            limits=RunLimits(
                max_model_turns=8,
                max_tool_calls=12,
                max_run_seconds=90,
                max_parallel_reads=3,
            ),
        )
        run_id = str(uuid4())
        result = await loop.run(
            run_id=run_id,
            messages=(
                SystemMessage((TextBlock(SYSTEM_PROMPT),)),
                UserMessage((TextBlock(clean_message),)),
            ),
            identity=identity,
            ktg_client=client,
        )

        answer = ""
        if result.final_message is not None:
            answer = "".join(
                block.text
                for block in result.final_message.content
                if isinstance(block, TextBlock)
            ).strip()
        if not answer:
            answer = "查询未生成最终回答。"

        business_results: list[JsonValue] = []
        for item in result.messages:
            if isinstance(item, ToolResultMessage) and item.details is not None:
                business_results.append(item.details)

        username = identity.get("userName")
        return {
            "run_id": run_id,
            "answer": answer,
            "stop_reason": result.stop_reason.value,
            "current_user": username if isinstance(username, str) else "",
            "results": business_results,
        }

