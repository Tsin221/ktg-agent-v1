"""Read-only real model stream probe; prints metadata without response text or key."""

from __future__ import annotations

import asyncio
import os
from time import perf_counter

from ktg_agent.core.messages import TextBlock, UserMessage
from ktg_agent.providers.base import (
    ModelConfig, ProviderRequest, RequestStarted, ResponseCompleted,
    TextDelta, UsageReported,
)
from ktg_agent.providers.openai_compatible import OpenAICompatibleProvider


async def main() -> None:
    model = os.environ["MODEL_ID"]
    provider = OpenAICompatibleProvider(
        api_key=os.environ["MODEL_API_KEY"],
        base_url=os.environ["MODEL_BASE_URL"],
    )
    max_output_tokens = int(os.environ.get("T23_PROBE_MAX_OUTPUT_TOKENS", "512"))
    request = ProviderRequest(
        messages=(UserMessage((TextBlock("请用一句简短中文回答：流式验收。"),)),),
        tools=(), config=ModelConfig(model, max_output_tokens),
    )
    started = perf_counter()
    print(f"model={model}", flush=True)
    delta_count = 0
    async for item in provider.stream(request):
        elapsed = round(perf_counter() - started, 3)
        if isinstance(item, RequestStarted):
            print(f"request_started_s={elapsed}", flush=True)
        elif isinstance(item, TextDelta):
            delta_count += 1
            print(f"delta_{delta_count}_s={elapsed} chars={len(item.text)}", flush=True)
        elif isinstance(item, UsageReported):
            print(f"usage_s={elapsed} input={item.usage.input_tokens} "
                  f"output={item.usage.output_tokens} "
                  f"cached_input={item.usage.cached_input_tokens}", flush=True)
        elif isinstance(item, ResponseCompleted):
            print(f"completed_s={elapsed} reason={item.response.stop_reason.value} "
                  f"usage_known={item.response.usage is not None}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
