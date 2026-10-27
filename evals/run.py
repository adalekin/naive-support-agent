"""Run the evaluation cases against the real model and grade the answers.

    cd server && uv run python ../evals/run.py                # every case
    cd server && uv run python ../evals/run.py --case model-refuses
    cd server && uv run python ../evals/run.py --check        # read the cases, call nothing

Every run costs money: the report ends with what it cost. It needs ANTHROPIC_API_KEY in the
environment and the PostgreSQL of compose.yaml (database `assistant`, migrated here).
"""

import argparse
import asyncio
import datetime
import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "server"))

from alembic import command
from alembic.config import Config
from anthropic import AsyncAnthropic
from sqlalchemy import URL, delete, select

from internal.access.guard import Staff
from internal.access.oidc import Identity
from internal.agent.commands import CaseContext
from internal.agent.prompt import system_prompt
from internal.agent.runner import CaseAgent, TurnOutcome
from internal.bot.outbox import Outbox, Send
from internal.config.instructions import InstructionSet
from internal.crypto import TokenCipher
from internal.db import Database
from internal.entity import Case, Incident, StaffBinding
from internal.privacy.pseudonymizer import LabelMap, Pseudonymizer
from internal.repository import CaseRepository, StaffRepository
from internal.sources.base import Source, SourceRegistry, ToolOutput, ToolSpec
from internal.words import Words

MODEL = "claude-opus-5-5"
EFFORT = "medium"
GRADER_EFFORT = "medium"
#: USD per million tokens for the model above: input, output, cache read, cache write (5 minutes).
PRICE = {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write": 5.0}
#: A key only for the evaluation's own rows: nothing outside the run reads them.
EVAL_KEY = "cOJ8i2b6Yk2QvKkH4jH9yH9QG8Dk0YJkGxq8bqvM7xU="
CHAT = -1009000
OWNER_TELEGRAM = 9000001
STAFF_TELEGRAM = 9000002

#: The sources every case has; a case scripts what they answer.
TOOLS = {
    "db": (
        "The product database, read only.",
        [ToolSpec("query", "Run a read-only SQL query.", {
            "type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]})],
    ),
    "errors": (
        "The error tracker of the web app and the services.",
        [ToolSpec("search", "Search errors by text and time.", {
            "type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]})],
    ),
    "code": (
        "The code of the product.",
        [
            ToolSpec("search", "Search the code for a regular expression.", {
                "type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}),
            ToolSpec("read", "Read a file of the code.", {
                "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}),
        ],
    ),
    "ui": (
        "A browser signed in to the test account of Pixmill: no money, no saved way to pay.",
        [ToolSpec("open", "Open a page and describe what is on it.", {
            "type": "object",
            "properties": {"url": {"type": "string"}, "view": {"type": "string", "enum": ["desktop", "mobile"]}},
            "required": ["url"]})],
    ),
    "tracker": (
        "The task tracker of the developers, read only.",
        [ToolSpec("search", "Search tasks by text.", {
            "type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]})],
    ),
}  # fmt: skip

GRADER_PROMPT = """\
You grade an answer that an assistant gave to a member of support staff about a client's case.
For every criterion, decide whether the answer meets it, and quote the words of the answer that
decide it. The assistant cannot change anything, file a task or refund: it proposes, and calls the
responsible person; a task "for the developers" in a criterion means a task it proposes. A "must" criterion is met when the answer contains what it describes. A "must not"
criterion is met when the answer does not contain what it describes. Judge only by the answer and
the messages to the responsible person; the case is there for context. A criterion about what
support reads applies to the answer to the staff only: the messages to the responsible person may
hold technical detail."""

GRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "criterion": {"type": "string"},
                    "met": {"type": "boolean"},
                    "evidence": {"type": "string"},
                },
                "required": ["criterion", "met", "evidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


@dataclass
class EvalCase:
    id: str
    title: str
    kind: str
    requirements: list[str]
    request: dict[str, Any]
    responses: list[dict[str, str]]
    expect: dict[str, Any]
    rubric: dict[str, list[str]]

    @classmethod
    def load(cls, path: Path) -> "EvalCase":
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        for response in data.get("responses", []):
            source, _, tool = response["tool"].partition("__")
            if source not in TOOLS or tool not in {spec.name for spec in TOOLS[source][1]}:
                raise ValueError(f"{path.name}: there is no tool {response['tool']}")
            re.compile(response["match"])
        if data["request"]["chat"] not in ("group", "private"):
            raise ValueError(f"{path.name}: request.chat is group or private")
        kinds = data["expect"]["kind"] if isinstance(data["expect"]["kind"], list) else [data["expect"]["kind"]]
        if not set(kinds) <= {"case", "reply", "question"}:
            raise ValueError(f"{path.name}: expect.kind is case, reply or question")
        if not data["rubric"]["must"]:
            raise ValueError(f"{path.name}: the rubric has no must")
        return cls(
            data["id"], data["title"], data["kind"], data["requirements"], data["request"],
            data.get("responses", []), data["expect"], data["rubric"],
        )  # fmt: skip


class ScriptedSource(Source):
    """A source that answers from the case: the first scripted response whose pattern matches the arguments."""

    def __init__(self, name: str, description: str, tools: list[ToolSpec], responses: list[dict[str, str]]) -> None:
        self.name = name
        self.description = description
        self._tools = tools
        self._responses = [r for r in responses if r["tool"].startswith(f"{name}__")]

    async def list_tools(self) -> list[ToolSpec]:
        return self._tools

    async def call(self, tool: str, arguments: dict[str, Any]) -> ToolOutput:
        text = json.dumps(arguments, ensure_ascii=False)
        for response in self._responses:
            if response["tool"] == f"{self.name}__{tool}" and re.search(response["match"], text, re.IGNORECASE):
                return ToolOutput(response["output"])
        return ToolOutput("[]")


class OneOwner:
    """The guard of the evaluation: one responsible person is bound, and the provider always answers."""

    def __init__(self, owner: Staff) -> None:
        self._owner = owner

    async def owners(self) -> list[Staff]:
        return [self._owner]


@dataclass
class Result:
    case: EvalCase
    outcome: TurnOutcome
    answer_text: str
    answer_kind: str | None
    owner_messages: list[str]
    state: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    grades: list[dict[str, Any]] = field(default_factory=list)
    grader_cost: float = 0.0

    @property
    def cost(self) -> float:
        return turn_cost(self.outcome) + self.grader_cost

    @property
    def passed(self) -> bool:
        return all(ok for _, ok, _ in self.checks) and all(grade["met"] for grade in self.grades)


def turn_cost(outcome: TurnOutcome) -> float:
    uncached = outcome.input_tokens - outcome.cache_read_tokens - outcome.cache_write_tokens
    return (
        uncached * PRICE["input"]
        + outcome.cache_read_tokens * PRICE["cache_read"]
        + outcome.cache_write_tokens * PRICE["cache_write"]
        + outcome.output_tokens * PRICE["output"]
    ) / 1_000_000


def usage_cost(usage: Any) -> float:
    read = usage.cache_read_input_tokens or 0
    write = usage.cache_creation_input_tokens or 0
    return (
        usage.input_tokens * PRICE["input"]
        + read * PRICE["cache_read"]
        + write * PRICE["cache_write"]
        + usage.output_tokens * PRICE["output"]
    ) / 1_000_000


async def run_case(case: EvalCase, number: int, database: Database, client: AsyncAnthropic, instructions) -> Result:
    cipher = TokenCipher(EVAL_KEY)
    registry = SourceRegistry(
        [ScriptedSource(name, description, tools, case.responses) for name, (description, tools) in TOOLS.items()],
        {name: [spec.name for spec in tools] for name, (_, tools) in TOOLS.items()},
    )
    await registry.start()
    async with database.unit_of_work() as session:
        staff_repo = StaffRepository(session)
        await staff_repo.bind(STAFF_TELEGRAM, "eval-staff", "anna", cipher.seal("-"), "anna")
        await staff_repo.bind(OWNER_TELEGRAM, "eval-owner", "oleg", cipher.seal("-"), "oleg")
        staff_binding = await staff_repo.by_telegram(STAFF_TELEGRAM)
        owner_binding = await staff_repo.by_telegram(OWNER_TELEGRAM)
        private = case.request["chat"] == "private"
        stored = await CaseRepository(session).open(STAFF_TELEGRAM if private else CHAT, number, private, "eval-staff")
    staff = Staff(staff_binding, Identity("eval-staff", "anna", frozenset({"support"})), True, False)
    owner = Staff(owner_binding, Identity("eval-owner", "oleg", frozenset({"support", "owner"})), True, True)

    labels = LabelMap()
    pseudonymizer = Pseudonymizer(instructions.personal_fields, labels)
    text = pseudonymizer.mask_text(case.request["text"])
    place = "a private chat" if private else "a work group chat"
    content = [{"type": "text", "text": f"From anna, a member of staff, in {place}.\n\nMessage:\n{text}"}]
    outbox = Outbox()
    context = CaseContext(
        database=database,
        case=stored,
        staff=staff,
        labels=labels,
        gateway=registry.gateway(pseudonymizer),
        guard=OneOwner(owner),  # type: ignore[arg-type]
        words=Words("en"),
        outbox=outbox,
        now=datetime.datetime.now(datetime.UTC),
        request_labels=set(re.findall(r"<<[a-z]+_\d+>>", text)),
        request_text=text,
        reply_to=number,
        thread_id=None,
    )
    agent = CaseAgent(client, MODEL, EFFORT, 400_000, system_prompt(instructions))
    outcome = await agent.run(context, content, None, {})
    await registry.close()

    answer = context.answer
    rendered = answer.render(labels, Words("en")) if answer else None
    # The answer is not in the outbox: what is there went to the responsible people, or says nobody could be called.
    owner_messages = [reply.text.text for reply in outbox.replies if isinstance(reply, Send)]
    result = Result(
        case=case,
        outcome=outcome,
        answer_text=rendered.text if rendered else f"(no answer: {outcome.failure})",
        answer_kind=answer.kind if answer else None,
        owner_messages=owner_messages,
        state=stored.state.value,
    )
    check(result, rendered, context.escalated)
    return result


def check(result: Result, rendered, escalated: bool) -> None:
    """What code can tell: the kind of the answer, the call to the responsible person, words that must not be
    there, and personal data out of the spoiler."""
    expect = result.case.expect
    checks = result.checks
    checks.append(("answered", rendered is not None, result.outcome.failure or ""))
    # A case may accept more than one kind: asking what to find out is as good as an answer about the case.
    kinds = expect["kind"] if isinstance(expect["kind"], list) else [expect["kind"]]
    checks.append(("kind", result.answer_kind in kinds, f"{result.answer_kind} for {' or '.join(kinds)}"))
    if "escalated" in expect:
        checks.append(("escalated", escalated == expect["escalated"], f"{escalated} for {expect['escalated']}"))
    # Words support must not read: the messages to the responsible person may hold technical detail.
    for word in expect.get("forbidden", []):
        checks.append((f"no '{word}'", word.lower() not in result.answer_text.lower(), ""))
    if rendered is not None:
        hidden = {
            rendered.text.encode("utf-16-le")[e.offset * 2 : (e.offset + e.length) * 2].decode("utf-16-le")
            for e in rendered.entities
            if e.type == "spoiler"
        }
        emails = set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", rendered.text))
        checks.append(("emails under spoiler", emails <= hidden, ", ".join(sorted(emails - hidden))))


async def grade(result: Result, client: AsyncAnthropic) -> None:
    rubric = [{"criterion": c, "kind": "must"} for c in result.case.rubric["must"]]
    rubric += [{"criterion": c, "kind": "must not"} for c in result.case.rubric.get("must_not", [])]
    owner = "\n\n".join(result.owner_messages) or "(none)"
    response = await client.messages.create(
        model=MODEL,
        max_tokens=8000,
        system=GRADER_PROMPT,
        output_config={"effort": GRADER_EFFORT, "format": {"type": "json_schema", "schema": GRADE_SCHEMA}},
        messages=[
            {
                "role": "user",
                "content": f"Case: {result.case.request['text']}\n\nAnswer to the staff:\n{result.answer_text}\n\n"
                f"Messages to the responsible person:\n{owner}\n\nCriteria:\n{json.dumps(rubric, ensure_ascii=False)}",
            }
        ],
    )
    result.grader_cost = usage_cost(response.usage)
    if response.stop_reason == "refusal":
        result.grades = [{"criterion": "grading", "met": False, "evidence": "the grader declined"}]
        return
    items = json.loads(next(block.text for block in response.content if block.type == "text"))["items"]
    kinds = {item["criterion"]: item["kind"] for item in rubric}
    result.grades = [{**item, "kind": kinds.get(item["criterion"], "?")} for item in items]


def report(results: list[Result]) -> str:
    lines = [
        f"# Evaluation {datetime.datetime.now(datetime.UTC):%Y-%m-%d %H:%M} UTC",
        "",
        f"Model {MODEL}, effort {EFFORT}.",
        "",
    ]
    by_case: dict[str, list[Result]] = {}
    for r in results:
        by_case.setdefault(r.case.id, []).append(r)
    lines += ["| case | runs passed | checks | rubric | cost |", "|---|---|---|---|---|"]
    for case_id, runs in by_case.items():
        checks = f"{sum(ok for r in runs for _, ok, _ in r.checks)}/{sum(len(r.checks) for r in runs)}"
        rubric = f"{sum(g['met'] for r in runs for g in r.grades)}/{sum(len(r.grades) for r in runs)}"
        cost = sum(r.cost for r in runs)
        lines.append(f"| {case_id} | {sum(r.passed for r in runs)}/{len(runs)} | {checks} | {rubric} | ${cost:.3f} |")
    lines += ["", f"Total cost: ${sum(r.cost for r in results):.2f}", ""]

    requirements: dict[str, list[bool]] = {}
    for r in results:
        for requirement in r.case.requirements:
            requirements.setdefault(requirement, []).append(r.passed)
    lines += ["## By requirement", "", "| requirement | runs passed |", "|---|---|"]
    for requirement, passed in sorted(requirements.items()):
        lines.append(f"| {requirement} | {sum(passed)}/{len(passed)} |")

    for r in results:
        run = by_case[r.case.id].index(r) + 1
        lines += [
            "",
            f"## {r.case.id}, run {run}: {r.case.title}",
            "",
            f"State: {r.state}. Answer ({r.answer_kind}):",
            "",
        ]
        lines += [f"> {line}" for line in r.answer_text.splitlines()]
        for message in r.owner_messages:
            lines += ["", "To the responsible person:", ""] + [f"> {line}" for line in message.splitlines()]
        lines += ["", "Checks:"]
        lines += [
            f"- {'✓' if ok else '✗'} {name}{f' ({note})' if note and not ok else ''}" for name, ok, note in r.checks
        ]
        lines += ["", "Rubric:"]
        lines += [f"- {'✓' if g['met'] else '✗'} [{g['kind']}] {g['criterion']} — {g['evidence']}" for g in r.grades]
    return "\n".join(lines) + "\n"


async def clean(database: Database) -> None:
    """The evaluation's rows go: every run starts clean, with no incident of an earlier one."""
    async with database.unit_of_work() as session:
        linked = select(Case.incident_id).where(Case.staff_subject == "eval-staff", Case.incident_id.is_not(None))
        incidents = list(await session.scalars(linked))
        await session.execute(delete(Case).where(Case.staff_subject == "eval-staff"))
        await session.execute(delete(Incident).where(Incident.id.in_(incidents)))
        await session.execute(delete(StaffBinding).where(StaffBinding.subject.in_(["eval-staff", "eval-owner"])))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", action="append", help="Run only these cases, by id.")
    parser.add_argument("--check", action="store_true", help="Read the cases and stop: nothing is called.")
    parser.add_argument("--repeat", type=int, default=1, help="Run every case this many times: answers vary.")
    arguments = parser.parse_args()

    cases = [EvalCase.load(path) for path in sorted((HERE / "cases").glob("*.toml"))]
    if arguments.case:
        unknown = set(arguments.case) - {case.id for case in cases}
        if unknown:
            sys.exit(f"No such cases: {', '.join(sorted(unknown))}")
        cases = [case for case in cases if case.id in arguments.case]
    instructions = InstructionSet.load(HERE / "instructions")
    if arguments.check:
        print(f"{len(cases)} cases read; instructions of {len(instructions.text)} characters.")
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set")

    url = URL.create(
        "postgresql+asyncpg",
        os.environ.get("EVAL_DB_USER", "assistant"),
        os.environ.get("EVAL_DB_PASSWORD", "assistant"),
        os.environ.get("EVAL_DB_HOST", "127.0.0.1"),
        int(os.environ.get("EVAL_DB_PORT", "54330")),
        os.environ.get("EVAL_DB_NAME", "assistant"),
    )
    config = Config(str(HERE.parent / "server" / "alembic.ini"))
    config.set_main_option("script_location", str(HERE.parent / "server" / "migrations"))
    config.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False))
    command.upgrade(config, "head")

    database = Database(url, TokenCipher(EVAL_KEY))
    client = AsyncAnthropic()
    results = []
    try:
        for index, case in enumerate(cases, start=1):
            for run in range(1, arguments.repeat + 1):
                print(f"{case.id}, run {run} ...", flush=True)
                result = await run_case(case, index * 1000 + run, database, client, instructions)
                await grade(result, client)
                results.append(result)
                await clean(database)
                print(f"  {'passed' if result.passed else 'failed'}, ${result.cost:.3f}", flush=True)
    finally:
        await clean(database)
        await database.dispose()

    out = HERE / "reports" / f"{datetime.datetime.now(datetime.UTC):%Y%m%d-%H%M%S}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(report(results), encoding="utf-8")
    print(f"\n{sum(r.passed for r in results)}/{len(results)} cases passed, ${sum(r.cost for r in results):.2f}; {out}")


if __name__ == "__main__":
    asyncio.run(main())
