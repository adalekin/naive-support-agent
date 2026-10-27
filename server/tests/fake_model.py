"""A scripted Messages API: each request gets the next reply; every request body is kept."""

import json
from typing import Any

import httpx2
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient


def tool_use(name: str, input: dict[str, Any], id: str | None = None) -> dict[str, Any]:
    return {"type": "tool_use", "id": id or f"toolu_{name}", "name": name, "input": input}


def reply(*content: dict[str, Any], stop_reason: str = "tool_use", input_tokens: int = 100, output_tokens: int = 50):
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "thinking", "thinking": "", "signature": "sig"}, *content],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


class FakeModel:
    def __init__(self, *replies: dict[str, Any]) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        return httpx2.Response(200, json=self.replies.pop(0))

    def client(self) -> AsyncAnthropic:
        return AsyncAnthropic(
            api_key="test-model-key",
            http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(self._handle)),
            max_retries=0,
        )

    def tool_results(self, index: int) -> list[dict[str, Any]]:
        """The tool results the request ``index`` carried back."""
        return [block for block in self.requests[index]["messages"][-1]["content"] if block["type"] == "tool_result"]
