"""The installation's instructions: a directory the installation mounts, read once at start.

    <INSTRUCTIONS_DIR>/
        *.md            how to investigate, where the data is, known failures, compensation rules,
                        language and tone; joined in the order of their names
        sources.toml    the sources the assistant reads and the fields that hold personal data

Nothing about the organization or its product lives in the code: it all comes from here.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from internal.config.installation import ConfigurationError

SOURCES_FILE = "sources.toml"
NAME_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class McpSourceSpec(_Strict):
    """An MCP server of the installation. Only the tools listed in ``tools`` are ever called."""

    kind: Literal["mcp"]
    name: str = Field(pattern=NAME_PATTERN)
    #: What the source is for, in the words the model reads.
    description: str = Field(min_length=1)
    #: stdio starts the server inside the container; http is streamable HTTP; sse the older server-sent events.
    transport: Literal["stdio", "http", "sse"]
    #: The command that starts a stdio server.
    command: list[str] | None = None
    #: The address of a network server.
    url: str | None = None
    #: Names of variables of the assistant's own environment handed to a stdio server: secrets stay out of the file.
    env: list[str] = []
    #: A variable of the assistant's environment with a token a network server asks for, sent as a bearer token.
    token_env: str | None = None
    tools: list[str] = Field(min_length=1)
    #: The source walks the product's interface under the installation's test account: a browser server
    #: started signed in to an account with no money and no saved way to pay.
    interface: bool = False
    #: Where the interface of the product opens; required for the source that walks it.
    product_url: str | None = None

    @model_validator(mode="after")
    def check_transport(self) -> "McpSourceSpec":
        if self.transport == "stdio":
            if not self.command:
                raise ValueError("a stdio source needs a command")
            if self.url or self.token_env:
                raise ValueError("a stdio source takes no url and no token_env: it is given env")
        else:
            if not self.url:
                raise ValueError(f"an {self.transport} source needs a url")
            if self.command or self.env:
                raise ValueError(f"an {self.transport} source takes no command and no env")
        if self.interface != (self.product_url is not None):
            raise ValueError("product_url is given exactly for the source that walks the interface")
        return self


class RepositorySourceSpec(_Strict):
    """A git repository of the product's code, kept as a local clone and only read."""

    kind: Literal["repository"]
    name: str = Field(pattern=NAME_PATTERN)
    description: str = Field(min_length=1)
    url: str = Field(min_length=1)
    branch: str = Field(min_length=1)
    #: A variable of the assistant's environment with a token that reads the repository, if it is private.
    token_env: str | None = None
    #: How often the clone is brought up to date.
    sync_minutes: int = Field(gt=0)


SourceSpec = Annotated[McpSourceSpec | RepositorySourceSpec, Field(discriminator="kind")]


class SourcesFile(_Strict):
    #: Names of fields in what the sources return that hold names or payment data: their values never reach the model.
    personal_fields: list[str]
    source: list[SourceSpec] = []

    @model_validator(mode="after")
    def check_names(self) -> "SourcesFile":
        names = [spec.name for spec in self.source]
        if len(set(names)) != len(names):
            raise ValueError("source names must not repeat")
        if sum(isinstance(spec, McpSourceSpec) and spec.interface for spec in self.source) > 1:
            raise ValueError("at most one source walks the interface")
        return self


@dataclass(frozen=True)
class InstructionSet:
    #: The text that opens the system prompt, unchanged between requests.
    text: str
    sources: tuple[McpSourceSpec | RepositorySourceSpec, ...]
    personal_fields: frozenset[str]

    @classmethod
    def load(cls, directory: Path) -> "InstructionSet":
        if not directory.is_dir():
            raise ConfigurationError(f"INSTRUCTIONS_DIR: {directory} is not a directory")
        texts = [path.read_text(encoding="utf-8").strip() for path in sorted(directory.glob("*.md"))]
        texts = [text for text in texts if text]
        if not texts:
            raise ConfigurationError(f"INSTRUCTIONS_DIR: {directory} holds no instructions (*.md files)")

        sources_path = directory / SOURCES_FILE
        if not sources_path.is_file():
            raise ConfigurationError(f"INSTRUCTIONS_DIR: {directory} has no {SOURCES_FILE}")
        try:
            sources = SourcesFile.model_validate(tomllib.loads(sources_path.read_text(encoding="utf-8")))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigurationError(f"{SOURCES_FILE}: {exc}") from exc
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()
            )
            raise ConfigurationError(f"{SOURCES_FILE}: {problems}") from exc

        return cls(
            text="\n\n".join(texts),
            sources=tuple(sources.source),
            personal_fields=frozenset(field.lower() for field in sources.personal_fields),
        )

    @property
    def has_interface(self) -> bool:
        return any(isinstance(spec, McpSourceSpec) and spec.interface for spec in self.sources)
