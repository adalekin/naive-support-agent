from collections.abc import Awaitable, Callable
from typing import Any

from anthropic.lib.tools import BetaAsyncBuiltinFunctionTool, ToolError
from anthropic.types.beta import BetaToolParam

ToolResult = str | list[dict[str, Any]]


class Refused(ToolError):
    """A command checked its input and refused it: the model gets the reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)


class RunnableTool(BetaAsyncBuiltinFunctionTool):
    """A tool the runner can call, built from a schema and a coroutine: a source tool or a command."""

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        run: Callable[[dict[str, Any]], Awaitable[ToolResult]],
        *,
        strict: bool = False,
    ) -> None:
        self._definition: BetaToolParam = {"name": name, "description": description, "input_schema": input_schema}
        if strict:
            self._definition["strict"] = True
        self._run = run

    def to_dict(self) -> BetaToolParam:
        return self._definition

    async def call(self, input: object) -> ToolResult:
        if not isinstance(input, dict):
            raise Refused("The input must be an object.")
        return await self._run(input)


def strict_object(properties: dict[str, Any]) -> dict[str, Any]:
    """A schema for strict tool use: every property required, nothing else allowed."""
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def nullable(kind: str, description: str) -> dict[str, Any]:
    return {"type": [kind, "null"], "description": description}
