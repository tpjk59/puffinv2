"""Agentic loop: LLM turn + tool dispatch, repeated until end_turn."""

import json

import anthropic
from sqlalchemy.ext.asyncio import AsyncSession

from agent.prompts import SYSTEM_PROMPT
from agent.tools import TOOL_DEFINITIONS, dispatch_tool

_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 4096

# System prompt is cached as an ephemeral prefix — saved on every subsequent turn.
_SYSTEM_CACHED = [
    {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
]


async def run_agent(
    user_message: str,
    session: AsyncSession,
    image_b64: str | None = None,
    media_type: str = "image/jpeg",
    history: list[dict] | None = None,
) -> str:
    """Run one conversational turn of the agent and return the final text response.

    history is a list of prior {"role": ..., "content": ...} messages prepended
    before the current turn so the model has conversational context.
    If image_b64 is provided it is prepended as an image block (photo workflow).
    The loop continues dispatching tools until stop_reason is 'end_turn'.
    """
    client = anthropic.AsyncAnthropic()

    if image_b64:
        content: list | str = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": image_b64},
            },
            {"type": "text", "text": user_message},
        ]
    else:
        content = user_message

    messages: list[dict] = list(history or [])
    messages.append({"role": "user", "content": content})

    while True:
        response = await client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM_CACHED,
            tools=TOOL_DEFINITIONS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            text_blocks = [b for b in response.content if b.type == "text"]
            return text_blocks[0].text if text_blocks else "(no response)"

        if response.stop_reason != "tool_use":
            return f"Unexpected stop reason: {response.stop_reason}"

        # Dispatch every tool_use block in this response
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            try:
                result = await dispatch_tool(block.name, block.input, session)
            except Exception as exc:  # noqa: BLE001
                result = {"error": str(exc)}
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                }
            )

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})
