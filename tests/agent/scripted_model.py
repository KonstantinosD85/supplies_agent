"""A fake Strands model that plays back a script, so agent tests make no LLM calls."""

import copy
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from strands.models import Model


@dataclass
class ToolUse:
    name: str
    input: dict[str, Any] = field(default_factory=dict)


def tool_call(name: str, **input: Any) -> ToolUse:
    return ToolUse(name, input)


class ScriptedModel(Model):
    """Each call to stream() plays the next turn: a reply string, tool call(s), or an exception.

    Records what the model was given on each call in `self.calls`.
    """

    def __init__(self, *turns: str | ToolUse | list[ToolUse] | Exception) -> None:
        self.turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> dict[str, Any]:
        return {}

    async def structured_output(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:
        raise NotImplementedError
        yield

    async def stream(
        self, messages, tool_specs=None, system_prompt=None, **kwargs: Any
    ) -> AsyncGenerator[dict[str, Any], None]:
        self.calls.append(
            {
                "messages": copy.deepcopy(messages),
                "tools": sorted(spec["name"] for spec in tool_specs or []),
                "system_prompt": system_prompt,
            }
        )
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        yield {"messageStart": {"role": "assistant"}}

        if isinstance(turn, str):
            yield {"contentBlockDelta": {"delta": {"text": turn}}}
            yield {"contentBlockStop": {}}
            stop_reason = "end_turn"
        else:
            uses = turn if isinstance(turn, list) else [turn]
            for i, use in enumerate(uses):
                tool_use_id = f"tool-{len(self.calls)}-{i}"
                yield {
                    "contentBlockStart": {
                        "start": {"toolUse": {"toolUseId": tool_use_id, "name": use.name}}
                    }
                }
                yield {
                    "contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(use.input)}}}
                }
                yield {"contentBlockStop": {}}
            stop_reason = "tool_use"

        yield {"messageStop": {"stopReason": stop_reason}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
                "metrics": {"latencyMs": 1},
            }
        }
