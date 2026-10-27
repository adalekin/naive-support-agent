import asyncio
import base64
import os
from pathlib import Path
from typing import Any

from loguru import logger

from internal.config.instructions import RepositorySourceSpec
from internal.sources.base import Source, SourceUnavailable, ToolOutput, ToolSpec

#: How much of a file or of a search the model gets at once.
MAX_LINES = 400
MAX_MATCHES = 200
GIT_TIMEOUT_SECONDS = 300


class RepositorySource(Source):
    """The product's code, read from a local clone: search, list, read. Nothing is run or written.

    The clone is the source's own copy; bringing it up to date is the only write, and it
    happens in ``sync``, never on a request of the model.
    """

    def __init__(self, spec: RepositorySourceSpec, root: Path) -> None:
        self.name = spec.name
        self.description = spec.description
        self._spec = spec
        self._clone = root / spec.name
        self._lock = asyncio.Lock()

    @property
    def sync_seconds(self) -> int:
        return self._spec.sync_minutes * 60

    async def start(self) -> None:
        await self.sync()

    @property
    def alive(self) -> bool:
        return (self._clone / ".git").is_dir()

    async def sync(self) -> None:
        async with self._lock:
            if not (self._clone / ".git").is_dir():
                self._clone.parent.mkdir(parents=True, exist_ok=True)
                await self._git(
                    "clone", "--depth=1", "--single-branch", f"--branch={self._spec.branch}", self._spec.url,
                    str(self._clone), cwd=self._clone.parent,
                )  # fmt: skip
            else:
                await self._git("fetch", "--depth=1", "origin", self._spec.branch, cwd=self._clone)
                await self._git("reset", "--hard", "FETCH_HEAD", cwd=self._clone)
        logger.info("Repository {} is up to date", self.name)

    async def list_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                "search",
                "Search the code for a regular expression (git grep). Returns file:line:text, at most "
                f"{MAX_MATCHES} matches.",
                {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "An extended regular expression."},
                        "path": {"type": "string", "description": "Limit to this directory or file, optional."},
                    },
                    "required": ["pattern"],
                },
            ),
            ToolSpec(
                "list",
                "List the files of a directory of the code.",
                {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            ),
            ToolSpec(
                "read",
                f"Read lines of a file of the code, at most {MAX_LINES} at once.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "start_line": {"type": "integer", "minimum": 1},
                    },
                    "required": ["path"],
                },
            ),
        ]

    async def call(self, tool: str, arguments: dict[str, Any]) -> ToolOutput:
        if tool == "search":
            target = self._inside(arguments.get("path") or ".")
            relative = str(target.relative_to(self._clone.resolve())) or "."
            out = await self._git(
                "grep",
                "-n",
                "-I",
                "-E",
                "--",
                str(arguments["pattern"]),
                relative,
                cwd=self._clone,
                allow_no_match=True,
            )
            lines = out.splitlines()
            more = f"\n… {len(lines) - MAX_MATCHES} more matches" if len(lines) > MAX_MATCHES else ""
            return ToolOutput("\n".join(lines[:MAX_MATCHES]) + more or "No matches.")
        if tool == "list":
            target = self._inside(arguments["path"])
            if not target.is_dir():
                return ToolOutput(f"{arguments['path']} is not a directory.", is_error=True)
            names = sorted(f"{entry.name}/" if entry.is_dir() else entry.name for entry in target.iterdir())
            return ToolOutput("\n".join(name for name in names if name != ".git/"))
        if tool == "read":
            target = self._inside(arguments["path"])
            if not target.is_file():
                return ToolOutput(f"{arguments['path']} is not a file.", is_error=True)
            start = max(1, int(arguments.get("start_line") or 1))
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
            chunk = lines[start - 1 : start - 1 + MAX_LINES]
            numbered = "\n".join(f"{start + index}: {line}" for index, line in enumerate(chunk))
            rest = len(lines) - (start - 1 + len(chunk))
            return ToolOutput(numbered + (f"\n… {rest} more lines" if rest > 0 else ""))
        return ToolOutput(f"There is no tool {tool}.", is_error=True)

    def _inside(self, path: str) -> Path:
        root = self._clone.resolve()
        target = (root / path).resolve()
        if target != root and root not in target.parents:
            raise SourceUnavailable(f"{path} is outside the repository")
        if ".git" in target.relative_to(root).parts:
            raise SourceUnavailable(f"{path} is outside the code")
        return target

    async def _git(self, *args: str, cwd: Path, allow_no_match: bool = False) -> str:
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp"), "GIT_TERMINAL_PROMPT": "0"}
        if self._spec.token_env is not None:
            token = os.environ.get(self._spec.token_env)
            if token is None:
                raise SourceUnavailable(f"{self._spec.token_env} is not set")
            # Through the environment, not the command line: the process list does not show it.
            basic = base64.b64encode(f"oauth2:{token}".encode()).decode()
            env |= {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
            }
        process = await asyncio.create_subprocess_exec(
            "git", *args, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(), GIT_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            process.kill()
            raise SourceUnavailable(f"git {args[0]} took too long") from exc
        if process.returncode == 1 and allow_no_match:
            return ""
        if process.returncode != 0:
            raise SourceUnavailable(f"git {args[0]} failed: {err.decode(errors='replace').strip()[:300]}")
        return out.decode(errors="replace")
