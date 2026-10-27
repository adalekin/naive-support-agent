import datetime
import json

import pytest

from internal.access.guard import AccessGuard, Staff
from internal.access.oidc import Identity, OidcProvider
from internal.agent.commands import CaseContext
from internal.agent.prompt import system_prompt
from internal.agent.runner import CaseAgent
from internal.bot.outbox import Outbox
from internal.config.instructions import InstructionSet, McpSourceSpec
from internal.crypto import TokenCipher
from internal.entity import CaseState
from internal.privacy.pseudonymizer import LabelMap, Pseudonymizer
from internal.repository import CaseRepository, StaffRepository
from internal.sources.base import Source, SourceRegistry, ToolOutput, ToolSpec
from internal.words import Words
from tests.conftest import TEST_ENVIRONMENT
from tests.fake_model import FakeModel, reply, tool_use

NOW = datetime.datetime(2026, 10, 27, 12, 0, tzinfo=datetime.UTC)
cipher = TokenCipher(TEST_ENVIRONMENT["ENCRYPTION_KEY"])
INSTRUCTIONS = InstructionSet(
    text="Payments are in the payments table.", sources=(), personal_fields=frozenset({"name"})
)


class Database(Source):
    name = "db"
    description = "The product database."

    def __init__(self):
        self.seen = []

    async def list_tools(self):
        return [
            ToolSpec("query", "Run a read-only query.", {"type": "object", "properties": {"sql": {"type": "string"}}})
        ]

    async def call(self, tool, arguments):
        self.seen.append(arguments)
        return ToolOutput(json.dumps([{"account": "acc-7", "email": "client@example.com", "name": "Ivan"}]))


@pytest.fixture
async def setup(database):
    source = Database()
    registry = SourceRegistry([source], {"db": ["query"]})
    await registry.start()
    return source, registry


async def run_turn(database, registry, model, text, *, owner=False, budget=100_000, root=10):
    async with database.unit_of_work() as session:
        await StaffRepository(session).bind(77 + root, f"sub-{root}", "anna", cipher.seal("t"), None)
        binding = await StaffRepository(session).by_telegram(77 + root)
        case = await CaseRepository(session).open(-1001, root, False, f"sub-{root}")
    labels = LabelMap()
    pseudonymizer = Pseudonymizer(INSTRUCTIONS.personal_fields, labels)
    masked = pseudonymizer.mask_text(text)
    staff = Staff(binding, Identity(f"sub-{root}", "anna", frozenset({"support"})), True, owner)
    context = CaseContext(
        database=database,
        case=case,
        staff=staff,
        labels=labels,
        gateway=registry.gateway(pseudonymizer),
        guard=AccessGuard(database, OidcProvider("https://idp.test", "c", "s", "roles"), cipher, "support", "owner"),
        words=Words("en"),
        outbox=Outbox(),
        now=NOW,
        request_labels=set(labels.to_dict()),
        request_text=masked,
        reply_to=root,
        thread_id=None,
    )
    agent = CaseAgent(model.client(), "claude-opus-5-5", "high", budget, system_prompt(INSTRUCTIONS))
    outcome = await agent.run(context, [{"type": "text", "text": masked}], None, {})
    return context, outcome


ANSWER = {
    "kind": "case",
    "client_saw": "The payment of <<email_1>> failed.",
    "cause": "The bank declined the card.",
    "whose_error": "Not the organization's.",
    "tell_client_now": "Try another card.",
    "next_steps": "Nothing on our side.",
    "text": None,
    "attachments": [],
}


async def test_a_turn_reads_a_source_identifies_the_client_and_answers(database, setup):
    source, registry = setup
    model = FakeModel(
        reply(tool_use("db__query", {"sql": "select * from users where email = '<<email_1>>'"})),
        reply(tool_use("identify_client", {"account": "acc-7", "found_by": "<<email_1>>"})),
        reply(tool_use("set_case_state", {"state": "resolved", "waiting_for": None})),
        reply(tool_use("submit_answer", ANSWER)),
        reply({"type": "text", "text": ""}, stop_reason="end_turn"),
    )
    context, outcome = await run_turn(database, registry, model, "client@example.com cannot pay")

    assert outcome.failure is None
    assert context.answer.parts["cause"] == "The bank declined the card."
    assert context.case.client_account == "acc-7"
    assert context.case.state is CaseState.RESOLVED
    # The source got the real email; the model never did.
    assert source.seen == [{"sql": "select * from users where email = 'client@example.com'"}]
    sent = json.dumps(model.requests)
    assert "client@example.com" not in sent and "Ivan" not in sent
    # The model is offered only the declared tools.
    names = {tool["name"] for tool in model.requests[0]["tools"]}
    assert names == {"db__query", "set_case_state", "identify_client", "link_incident", "escalate",
                     "list_open_cases", "submit_answer"}  # fmt: skip
    assert model.requests[0]["fallbacks"] == "default"
    # The system prompt and the conversation are both read from the prompt cache.
    assert model.requests[0]["cache_control"] == {"type": "ephemeral"}
    assert model.requests[0]["system"][-1]["cache_control"] == {"type": "ephemeral"}


async def test_conversation_is_only_appended_to(database, setup):
    _, registry = setup
    reply_answer = {**ANSWER, "kind": "reply", "text": "ok"}
    reply_answer.update(dict.fromkeys(["client_saw", "cause", "whose_error", "tell_client_now", "next_steps"]))
    model = FakeModel(
        reply(tool_use("submit_answer", reply_answer)), reply({"type": "text", "text": ""}, stop_reason="end_turn")
    )
    context, _ = await run_turn(database, registry, model, "hello")
    first_turn = list(context.case.conversation)
    assert [message["role"] for message in first_turn] == ["user", "assistant", "user", "assistant"]
    # Every block the model returned is kept as it came, thinking with its signature too.
    assert first_turn[1]["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig"}


async def test_a_client_not_found_by_the_request_is_refused(database, setup):
    _, registry = setup
    model = FakeModel(
        reply(tool_use("identify_client", {"account": "acc-9", "found_by": "<<email_1>>"})),
        reply({"type": "text", "text": ""}, stop_reason="end_turn"),
    )
    context, outcome = await run_turn(database, registry, model, "client@example.com cannot pay")
    assert model.tool_results(1)[0]["is_error"] is True
    assert context.case.client_account is None
    assert outcome.failure == "the model gave no answer"


async def test_the_budget_stops_the_turn_and_keeps_the_conversation_valid(database, setup):
    _, registry = setup
    model = FakeModel(reply(tool_use("db__query", {"sql": "select 1"}), input_tokens=900, output_tokens=200))
    context, outcome = await run_turn(database, registry, model, "x", budget=1000)
    assert outcome.failure == "the token budget of the case ran out"
    last = context.case.conversation[-1]
    assert last["role"] == "user" and last["content"][0]["type"] == "tool_result"
    assert outcome.input_tokens == 900


async def test_the_summary_is_offered_only_to_a_responsible_person(database, setup):
    _, registry = setup
    end = reply({"type": "text", "text": ""}, stop_reason="end_turn")
    staff_model, owner_model = FakeModel(end), FakeModel(dict(end))
    await run_turn(database, registry, staff_model, "summary please")
    await run_turn(database, registry, owner_model, "summary please", owner=True, root=11)
    assert "summary" not in {tool["name"] for tool in staff_model.requests[0]["tools"]}
    assert "summary" in {tool["name"] for tool in owner_model.requests[0]["tools"]}


def test_system_prompt_does_not_change_between_cases():
    assert system_prompt(INSTRUCTIONS) == system_prompt(INSTRUCTIONS)
    assert system_prompt(INSTRUCTIONS)[0]["text"] == INSTRUCTIONS.text


def test_interface_rules_follow_the_installation():
    with_interface = InstructionSet(
        text="x",
        sources=(
            McpSourceSpec(
                kind="mcp",
                name="ui",
                description="Browser.",
                transport="http",
                url="http://b",
                tools=["open"],
                interface=True,
                product_url="https://app.example.com",
            ),
        ),
        personal_fields=frozenset(),
    )
    assert "https://app.example.com" in system_prompt(with_interface)[1]["text"]
    assert "cannot open the product's interface" in system_prompt(INSTRUCTIONS)[1]["text"]
