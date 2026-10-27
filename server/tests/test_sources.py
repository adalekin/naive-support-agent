import json
import sys
from pathlib import Path

import pytest

from internal.config.instructions import McpSourceSpec
from internal.privacy.pseudonymizer import LabelMap, Pseudonymizer
from internal.sources.base import Source, SourceGateway, SourceRegistry, SourceUnavailable, ToolOutput, ToolSpec
from internal.sources.mcp import McpSource

SERVER = str(Path(__file__).with_name("mcp_server.py"))


def spec(tools):
    return McpSourceSpec(
        kind="mcp",
        name="database",
        description="The product database.",
        transport="stdio",
        command=[sys.executable, SERVER],
        env=["TEST_SOURCE_LOG", "SOURCE_TOKEN"],
        tools=tools,
    )


@pytest.fixture
async def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_SOURCE_LOG", str(tmp_path / "log"))
    monkeypatch.setenv("SOURCE_TOKEN", "read-only-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "assistant-secret")
    registry = SourceRegistry([McpSource(spec(["find_user", "whoami"]))], {"database": ["find_user", "whoami"]})
    await registry.start()
    yield registry
    await registry.close()


def pseudonymizer():
    return Pseudonymizer(frozenset({"full_name"}), LabelMap())


async def test_only_listed_tools_are_offered(registry):
    assert set(registry.tools) == {"database__find_user", "database__whoami"}


async def test_a_write_tool_not_listed_is_never_called(registry, tmp_path):
    output = await registry.gateway(pseudonymizer()).call("database__tidy_up", {"email": "client@example.com"})
    assert output.is_error
    assert not (tmp_path / "log").exists()


async def test_answer_reaches_the_model_masked_and_the_arguments_unmasked(registry):
    p = pseudonymizer()
    label = p.mask_text("client@example.com")
    output = await registry.gateway(p).call("database__find_user", {"email": label})
    rows = json.loads(output.text)
    assert rows[0]["id"] == 7 and rows[0]["plan"] == "pro"
    assert "client@example.com" not in output.text and "Иван" not in output.text


async def test_a_source_gets_only_the_variables_named_for_it(registry):
    output = await registry.gateway(pseudonymizer()).call("database__whoami", {})
    given = json.loads(output.text)
    assert given == {"token": "read-only-token", "assistant_key": None}


class Broken(Source):
    name = "logs"
    description = "Service logs."

    async def list_tools(self):
        return [ToolSpec("search", "Search logs.", {"type": "object", "properties": {}})]

    async def call(self, tool, arguments):
        raise SourceUnavailable("connection refused by logs.internal")


async def test_unavailable_source_is_told_to_the_model_by_name():
    registry = SourceRegistry([Broken()], {"logs": ["search"]})
    await registry.start()
    output = await registry.gateway(pseudonymizer()).call("logs__search", {})
    assert output.is_error and "logs" in output.text and "connection refused" in output.text


async def test_calls_are_recorded():
    seen = []

    async def record(call):
        seen.append(call)

    class Fine(Broken):
        async def call(self, tool, arguments):
            return ToolOutput("ok")

    gateway = SourceGateway(
        {"logs": Fine()}, {"logs__search": ToolSpec("logs__search", "", {})}, pseudonymizer(), record
    )
    await gateway.call("logs__search", {})
    assert [(c.source, c.tool, c.ok) for c in seen] == [("logs", "search", True)]


class FailsToStart(Broken):
    name = "logs"
    attempts = 0

    async def start(self):
        FailsToStart.attempts += 1
        if FailsToStart.attempts == 1:
            raise SourceUnavailable("connection refused")


async def test_a_source_down_at_start_does_not_stop_the_others_and_is_started_again(registry):
    other = SourceRegistry(
        [FailsToStart(), McpSource(spec(["find_user"]))], {"logs": ["search"], "database": ["find_user"]}
    )
    await other.start()
    try:
        assert set(other.tools) == {"database__find_user"}
        assert "logs (connection refused)" in other.unavailable_note()
        await other.restart_down()
        assert set(other.tools) == {"database__find_user", "logs__search"}
        assert other.unavailable_note() is None
    finally:
        await other.close()
