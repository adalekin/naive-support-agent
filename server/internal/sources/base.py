"""The sources the assistant reads, behind one gateway.

Every source is an Adapter with the same face, ``Source``; ``SourceRegistry`` builds them from
the installation's ``sources.toml``; ``SourceGateway`` is the Proxy the model talks to. Writing
is refused on three levels, each on its own: the installation gives read-only access, the
gateway lets through only the tools the installation listed, and no adapter has a way to write.
"""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from internal.privacy.pseudonymizer import Pseudonymizer

#: The model sees a tool of a source as ``<source>__<tool>``.
SEPARATOR = "__"


class SourceUnavailable(Exception):
    """The source cannot answer now. The message says why, without secrets."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class Image:
    media_type: str
    data: bytes


@dataclass(frozen=True)
class ToolOutput:
    text: str
    images: tuple[Image, ...] = ()
    is_error: bool = False


class Source(ABC):
    name: str
    #: What the source is for, in the words the model reads.
    description: str

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    @abstractmethod
    async def list_tools(self) -> list[ToolSpec]: ...

    @abstractmethod
    async def call(self, tool: str, arguments: dict[str, Any]) -> ToolOutput: ...


@dataclass
class CallRecord:
    source: str
    tool: str
    ok: bool


@dataclass
class SourceGateway:
    """What the model calls for one turn of one case."""

    sources: dict[str, Source]
    #: Tool names the model may call, qualified with the source.
    tools: dict[str, ToolSpec]
    pseudonymizer: Pseudonymizer
    #: Called after every call, to journal it.
    on_call: Callable[[CallRecord], Awaitable[None]] | None = None
    calls: list[CallRecord] = field(default_factory=list)
    #: What the sources answered in this turn, masked: a client is identified only by what was found.
    outputs: list[str] = field(default_factory=list)

    async def call(self, qualified: str, arguments: dict[str, Any]) -> ToolOutput:
        if qualified not in self.tools:
            # Not offered to the model, so not called, whatever the model asks.
            return ToolOutput(f"There is no tool {qualified}.", is_error=True)
        source_name, tool = qualified.split(SEPARATOR, 1)
        source = self.sources[source_name]
        try:
            output = await source.call(tool, self.pseudonymizer.unmask(arguments))
        except SourceUnavailable as unavailable:
            logger.warning("Source {} is unavailable: {}", source_name, unavailable)
            output = ToolOutput(f"The source {source_name} is unavailable: {unavailable}", is_error=True)
        masked = ToolOutput(self.pseudonymizer.mask_source_text(output.text), output.images, output.is_error)
        record = CallRecord(source_name, tool, not output.is_error)
        self.calls.append(record)
        if not masked.is_error:
            self.outputs.append(masked.text)
        if self.on_call is not None:
            await self.on_call(record)
        return masked


class SourceRegistry:
    """The sources of the installation, started once and shared by every case.

    A source that cannot start, or loses its connection, does not stop the others or the
    assistant: it is reported as unavailable by name and started again later.
    """

    def __init__(self, sources: list[Source], allowed: dict[str, list[str]]) -> None:
        self._sources = {source.name: source for source in sources}
        self._allowed = allowed
        self._tools: dict[str, dict[str, ToolSpec]] = {}
        #: Name to why it is unavailable.
        self.down: dict[str, str] = {}

    async def start(self) -> None:
        for source in self._sources.values():
            await self._start(source)

    async def restart_down(self) -> None:
        for source in self._sources.values():
            if source.name in self.down or not source.alive:
                await source.close()
                await self._start(source)

    async def _start(self, source: Source) -> None:
        try:
            await source.start()
            offered = {tool.name: tool for tool in await source.list_tools()}
        except SourceUnavailable as unavailable:
            logger.warning("Source {} is unavailable: {}", source.name, unavailable)
            self.down[source.name] = str(unavailable)
            return
        self.down.pop(source.name, None)
        allowed = self._allowed.get(source.name)
        tools = {}
        for name in offered if allowed is None else allowed:
            if name not in offered:
                logger.warning("Source {} does not offer the listed tool {}", source.name, name)
                continue
            spec = offered[name]
            qualified = f"{source.name}{SEPARATOR}{name}"
            tools[qualified] = ToolSpec(
                qualified, f"[{source.name}: {source.description}] {spec.description}", spec.input_schema
            )
        self._tools[source.name] = tools

    async def close(self) -> None:
        for source in self._sources.values():
            await source.close()

    @property
    def tools(self) -> dict[str, ToolSpec]:
        """The tools of the sources that are up: the same set for every case."""
        return {
            qualified: spec
            for name, tools in sorted(self._tools.items())
            if name not in self.down
            for qualified, spec in tools.items()
        }

    def unavailable_note(self) -> str | None:
        if not self.down:
            return None
        listed = "; ".join(f"{name} ({reason})" for name, reason in sorted(self.down.items()))
        return f"Sources unavailable now: {listed}. Say which you could not check."

    def gateway(
        self, pseudonymizer: Pseudonymizer, on_call: Callable[[CallRecord], Awaitable[None]] | None = None
    ) -> SourceGateway:
        return SourceGateway(self._sources, self.tools, pseudonymizer, on_call)
