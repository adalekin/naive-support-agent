import asyncio
import base64
import os
from contextlib import AsyncExitStack
from typing import Any

import anyio
from loguru import logger
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from internal.config.instructions import McpSourceSpec
from internal.sources.base import Image, Source, SourceUnavailable, ToolOutput, ToolSpec

CALL_TIMEOUT_SECONDS = 120.0


class McpSource(Source):
    """An MCP server of the installation: a database, logs, errors, a tracker, a browser.

    The session is opened once and kept. Which of its tools may be called is decided by the
    gateway from the installation's list, not here.
    """

    def __init__(self, spec: McpSourceSpec) -> None:
        self.name = spec.name
        self.description = spec.description
        self._spec = spec
        self._task: asyncio.Task[None] | None = None
        self._session: ClientSession | None = None

    async def start(self) -> None:
        # The transport's task groups must be entered and left by one task: a task of the
        # source's own holds the session open until ``close``.
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._failure: BaseException | None = None
        self._task = asyncio.create_task(self._hold(), name=f"source-{self.name}")
        await self._ready.wait()
        if self._failure is not None:
            await self._task
            raise SourceUnavailable(f"cannot start: {type(self._failure).__name__}") from self._failure

    async def _hold(self) -> None:
        try:
            async with AsyncExitStack() as stack:
                if self._spec.transport == "stdio":
                    command, *args = self._spec.command or []
                    env = get_default_environment()
                    # Only what the installation named is handed over: the assistant's own secrets stay with it.
                    env.update({name: os.environ[name] for name in self._spec.env if name in os.environ})
                    missing = [name for name in self._spec.env if name not in os.environ]
                    if missing:
                        logger.warning("Source {} is started without {}", self.name, ", ".join(missing))
                    streams = await stack.enter_async_context(
                        stdio_client(StdioServerParameters(command=command, args=args, env=env))
                    )
                else:
                    streams = await stack.enter_async_context(streamable_http_client(self._spec.url or ""))
                session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
                await session.initialize()
                self._session = session
                self._ready.set()
                await self._stop.wait()
        except (OSError, MCPError) as exc:
            self._failure = exc
        finally:
            self._session = None
            self._ready.set()

    @property
    def alive(self) -> bool:
        return self._session is not None

    async def close(self) -> None:
        if self._task is not None:
            self._stop.set()
            await self._task
            self._task = None

    async def list_tools(self) -> list[ToolSpec]:
        session = self._require()
        listed = await session.list_tools()
        return [ToolSpec(tool.name, tool.description or "", dict(tool.input_schema)) for tool in listed.tools]

    async def call(self, tool: str, arguments: dict[str, Any]) -> ToolOutput:
        session = self._require()
        try:
            result = await session.call_tool(tool, arguments, read_timeout_seconds=CALL_TIMEOUT_SECONDS)
        except MCPError as exc:
            raise SourceUnavailable(exc.error.message) from exc
        except (OSError, anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream) as exc:
            # The connection is gone: the registry starts the source again.
            self._session = None
            self._stop.set()
            raise SourceUnavailable(f"the connection is lost: {type(exc).__name__}") from exc
        if not isinstance(result, types.CallToolResult):
            raise SourceUnavailable("the server asked for input the assistant cannot give")

        texts: list[str] = []
        images: list[Image] = []
        for block in result.content:
            if isinstance(block, types.TextContent):
                texts.append(block.text)
            elif isinstance(block, types.ImageContent):
                images.append(Image(block.mime_type, base64.b64decode(block.data)))
        return ToolOutput("\n".join(texts), tuple(images), bool(result.is_error))

    def _require(self) -> ClientSession:
        if self._session is None:
            raise SourceUnavailable("not started")
        return self._session
