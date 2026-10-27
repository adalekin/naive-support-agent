"""One turn of a case: the model, the tools the service declared, and nothing else."""

import base64
from dataclasses import dataclass
from typing import Any

from anthropic import AsyncAnthropic
from anthropic.lib.tools import ToolError
from loguru import logger

from internal.agent.commands import CaseCommands, CaseContext
from internal.agent.tools import RunnableTool, ToolResult
from internal.sources.base import SourceGateway, ToolSpec

MAX_TOKENS = 16000
#: The server re-runs a declined request on a fallback model chosen by the kind of decline.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
#: The server summarises a long conversation in place of its history.
COMPACTION_BETA = "compact-2026-09-04"
#: A request of a turn this large has the conversation compacted once the model's turn is over.
COMPACT_AT_TOKENS = 150_000


@dataclass(frozen=True)
class TurnOutcome:
    #: Why there is no answer, in plain words, when there is none.
    failure: str | None
    #: All input of the turn, read from the cache or not: what the budget counts.
    input_tokens: int
    output_tokens: int
    #: Of the input, how much was read from the prompt cache and how much was written to it.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class CaseAgent:
    """Runs the model over a case with the Tool Runner.

    The conversation of a case is only ever appended to: thinking blocks are bound to the
    conversation that produced them and break when an earlier turn changes.
    """

    def __init__(
        self,
        client: AsyncAnthropic,
        model: str,
        effort: str,
        token_budget: int,
        system: list[dict[str, Any]],
        compact_at: int = COMPACT_AT_TOKENS,
    ) -> None:
        self._client = client
        self._model = model
        self._effort = effort
        self._budget = token_budget
        self._system = system
        self._compact_at = compact_at

    async def run(
        self,
        context: CaseContext,
        user_content: list[dict[str, Any]],
        operator_note: str | None,
        images: dict[str, bytes],
    ) -> TurnOutcome:
        history: list[dict[str, Any]] = list(context.case.conversation)
        history.append({"role": "user", "content": user_content})
        if operator_note is not None:
            history.append({"role": "system", "content": operator_note})

        tools = [*self._source_tools(context.gateway, images), *CaseCommands(context).tools()]
        runner = self._client.beta.messages.tool_runner(
            model=self._model,
            max_tokens=MAX_TOKENS,
            system=self._system,
            tools=tools,
            messages=list(history),
            thinking={"type": "adaptive"},
            output_config={"effort": self._effort},
            fallbacks="default",
            betas=[FALLBACK_BETA, COMPACTION_BETA],
            # The cache point moves to the end of the conversation with every request: the next
            # request of the turn, and the next turn of the case within the cache's lifetime, read
            # the history from the cache instead of paying for it again.
            cache_control={"type": "ephemeral"},
        )

        spent_in = spent_out = cache_read = cache_write = 0
        failure: str | None = None
        last = None
        compacting = False
        async for message in runner:
            usage = message.usage
            request_in = (usage.input_tokens or 0) + (usage.cache_read_input_tokens or 0)
            request_in += usage.cache_creation_input_tokens or 0
            cache_read += usage.cache_read_input_tokens or 0
            cache_write += usage.cache_creation_input_tokens or 0
            spent_in += request_in
            spent_out += usage.output_tokens or 0

            content = {"role": "assistant", "content": [_block(block) for block in message.content]}
            if message.stop_reason == "compaction":
                # The summary takes the place of the whole history, on the server and here alike.
                history = [content]
                compacting = False
                continue
            last = message
            history.append(content)
            if request_in > self._compact_at and not compacting:
                runner.compact_before_next_turn()
                compacting = True

            if spent_in + spent_out > self._budget:
                failure = "the token budget of the case ran out"
                _close_open_calls(history, failure)
                break
            response = await runner.generate_tool_call_response()
            if response is not None:
                history.append(dict(response))

        if failure is None and last is not None and last.stop_reason == "refusal":
            failure = "the model declined the request"
        if failure is None and context.answer is None:
            failure = "the model gave no answer"
            if last is not None and last.stop_reason == "max_tokens":
                failure = "the answer did not fit"

        # A new list, so that the change of the JSON column is seen when the case is saved.
        context.case.conversation = history
        if failure is not None:
            logger.warning("Case {} has no answer: {}", context.case.id, failure)
        return TurnOutcome(failure, spent_in, spent_out, cache_read, cache_write)

    @staticmethod
    def _source_tools(gateway: SourceGateway, images: dict[str, bytes]) -> list[RunnableTool]:
        def runner_for(spec: ToolSpec):
            async def run(arguments: dict[str, Any]) -> ToolResult:
                output = await gateway.call(spec.name, arguments)
                content: list[dict[str, Any]] = [{"type": "text", "text": output.text or "(empty)"}]
                for image in output.images:
                    key = f"img_{len([name for name in images if name.startswith('img_')]) + 1}"
                    images[key] = image.data
                    content.append({"type": "text", "text": f"Picture {key}:"})
                    content.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": image.media_type,
                                "data": base64.standard_b64encode(image.data).decode(),
                            },
                        }
                    )
                if output.is_error:
                    raise ToolError(content)
                return content

            return run

        return [
            RunnableTool(spec.name, spec.description, spec.input_schema, runner_for(spec))
            for spec in gateway.tools.values()
        ]


def _block(block: Any) -> dict[str, Any]:
    return block.model_dump(mode="json", exclude_none=True, exclude={"parsed_output"})


def _close_open_calls(history: list[dict[str, Any]], reason: str) -> None:
    """The turn stops after a request for tools: their results are given as refused, so that the
    conversation stays one the API accepts on the next turn."""
    calls = [block for block in history[-1]["content"] if block.get("type") == "tool_use"]
    if calls:
        history.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": call["id"],
                        "content": f"Stopped: {reason}.",
                        "is_error": True,
                    }
                    for call in calls
                ],
            }
        )
