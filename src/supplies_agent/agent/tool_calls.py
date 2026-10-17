"""The tool calls of one invocation: streamed as they start and finish, then reported."""

from typing import Any

from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry
from strands.types.content import Messages
from strands.types.tools import ToolResult

from supplies_agent.agent.schemas import Emit, ToolCall, ToolFinished, ToolStarted

MAX_RESULT_CHARS = 2000


class ToolCalls(HookProvider):
    """Emits each tool call as it starts and finishes, and reports them when the invocation ends.

    When the model calls several tools at once and one of them waits for approval, Strands
    runs the others but keeps their results out of agent.messages until the invocation resumes.
    Keeping them here lets the report show them as done instead of waiting.
    """

    def __init__(self, emit: Emit) -> None:
        self.emit = emit
        self.finished: dict[str, ToolResult] = {}

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.on_start)
        registry.add_callback(AfterToolCallEvent, self.on_finish)

    def on_start(self, event: BeforeToolCallEvent) -> None:
        use = event.tool_use
        self.emit(ToolStarted(id=use["toolUseId"], name=use["name"], input=use["input"]))

    def on_finish(self, event: AfterToolCallEvent) -> None:
        tool_use_id = event.tool_use["toolUseId"]
        self.finished[tool_use_id] = event.result  # a retry overwrites
        self.emit(
            ToolFinished(
                id=tool_use_id, status=event.result["status"], result=result_text(event.result)
            )
        )

    def report(self, messages: Messages, start: int) -> list[ToolCall]:
        """Tool calls made or finished in this invocation (messages[start:]), in call order.

        A call made in this invocation may have no result yet: it waits for approval. A call
        can also start in an earlier invocation: an approved order's toolUse was proposed in the
        previous one and only runs now. Results finished in an earlier invocation are not
        reported again.
        """
        uses = {
            block["toolUse"]["toolUseId"]: block["toolUse"]
            for message in messages
            for block in message["content"]
            if "toolUse" in block
        }
        new_blocks = [block for message in messages[start:] for block in message["content"]]
        new_uses = [block["toolUse"]["toolUseId"] for block in new_blocks if "toolUse" in block]
        new_results = {
            block["toolResult"]["toolUseId"]: block["toolResult"]
            for block in new_blocks
            if "toolResult" in block
        }
        # Calls resumed from an earlier invocation ran first, then the ones made in this one.
        ids = [i for i in self.finished if i not in new_uses] + new_uses

        calls = []
        for tool_use_id in ids:
            use = uses.get(tool_use_id)
            if use is None:
                continue
            call = ToolCall(name=use["name"], input=use["input"])
            result = self.finished.get(tool_use_id) or new_results.get(tool_use_id)
            if result is not None:
                call.status = result["status"]
                call.result = result_text(result)
            calls.append(call)
        return calls


def result_text(result: ToolResult) -> str:
    """A tool result's text, truncated the same way for the stream and the report."""
    text = "\n".join(c["text"] for c in result["content"] if "text" in c)
    return text[:MAX_RESULT_CHARS]
