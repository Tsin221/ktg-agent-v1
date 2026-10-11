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
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from ktg_agent.ktg import (
    KTGClient,
    PreviewError,
    PreviewStore,
    build_query_tools,
    build_workorder_tools,
)
from ktg_agent.providers.base import ModelConfig, Provider
from ktg_agent.tools import ToolContext, ToolRegistry, ToolRunner


type KTGClientFactory = Callable[[str], KTGClient]


SYSTEM_PROMPT = """你是 KTG MES 的业务助手。
必须通过已注册工具查询事实，不得猜测产品、库存或工单数据。
所有查询都使用当前网页登录用户的身份和权限；权限不足时如实说明，不能改用其他身份。
允许查询产品资料、分仓库存和生产工单，也可以为用户明确要求的工单草稿创建或编辑生成预览。
创建或编辑前必须调用对应预览工具；产品不唯一时必须让用户按编码选择，不能自行猜测。
预览工具不会写入。实际写入只能由网页上的确认操作触发，不能要求用户在对话中提供确认令牌，不能声称自己已经写入。
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
        self._preview_store = PreviewStore(ttl_seconds=600)
        self._registry = ToolRegistry(
            (*build_query_tools(), *build_workorder_tools(self._preview_store))
        )

    async def query(self, *, message: str, authorization: str) -> JsonObject:
        clean_message = message.strip()
        if not clean_message:
            raise ValueError("查询内容不能为空。")
        if len(clean_message) > 2000:
            raise ValueError("查询内容不能超过 2000 个字符。")

        client = self._ktg_client_factory(authorization)
        identity = await client.get_current_user()
        loop = AgentLoop(
            provider=self._provider,
            tool_registry=self._registry,
            tool_runner=ToolRunner(self._registry),
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

    async def confirm(self, *, preview_id: str, authorization: str) -> JsonObject:
        clean_preview_id = preview_id.strip()
        if not clean_preview_id or len(clean_preview_id) > 100:
            raise ValueError("preview_id 无效。")
        client = self._ktg_client_factory(authorization)
        identity = await client.get_current_user()
        user_key = self._identity_key(identity)
        try:
            action = self._preview_store.action_for(
                preview_id=clean_preview_id, user_key=user_key
            )
        except PreviewError as exc:
            return {
                "result": {
                    "kind": "workorder_draft_result",
                    "outcome": "failed",
                    "label": str(exc),
                    "error_code": exc.code,
                    "preview_id": clean_preview_id,
                }
            }
        tool_name = (
            "ktg_create_workorder_draft"
            if action == "create"
            else "ktg_edit_workorder_draft"
        )
        call_id = str(uuid4())
        result = await ToolRunner(self._registry).run(
            ToolCall(call_id, tool_name, {"preview_id": clean_preview_id}),
            ToolContext(
                run_id=str(uuid4()),
                tool_call_id=call_id,
                operation_id=clean_preview_id,
                identity=identity,
                ktg_client=client,
            ),
        )
        details = result.details or {
            "kind": "workorder_draft_result",
            "outcome": "failed" if result.is_error else "succeeded",
            "label": result.content,
            "preview_id": clean_preview_id,
        }
        if result.error_code is not None:
            details["error_code"] = result.error_code
        username = identity.get("userName")
        return {
            "current_user": username if isinstance(username, str) else "",
            "result": details,
        }

    @staticmethod
    def _identity_key(identity: JsonObject) -> str:
        user_id = identity.get("userId")
        if isinstance(user_id, int):
            return f"id:{user_id}"
        username = identity.get("userName")
        if isinstance(username, str) and username.strip():
            return f"name:{username.strip()}"
        raise ValueError("KTG 未返回有效用户身份。")

