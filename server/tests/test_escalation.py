import pytest
from sqlalchemy import select

from internal.entity import Incident, JournalEntry, JournalKind
from tests.fake_model import FakeModel, reply, tool_use
from tests.harness import GROUP, Harness
from tests.test_requests import ANNA, NO_PARTS, OLEG, answer

ESCALATION = {
    "complaint": "<<email_1>> was charged but got no result.",
    "checked": "Payments and results of the account.",
    "found": "The charge exists, the result does not.",
    "question": "Refund or rerun?",
}


def escalating(cause=None):
    steps = []
    if cause:
        steps.append(
            reply(tool_use("link_incident", {"cause": cause, "description": "Results are lost after charge."}))
        )
    steps.append(reply(tool_use("escalate", ESCALATION)))
    return [*steps, *answer("Called the responsible person.", state="waiting_owner")]


@pytest.fixture
def model():
    return FakeModel()


async def harness(database, httpx_mock, model, owners=True, groups=frozenset({GROUP})):
    bot = await Harness(database, httpx_mock, model, groups=groups).start()
    await bot.bind(ANNA, "sub-anna", "anna_s")
    if owners:
        await bot.bind(OLEG, "sub-oleg", "oleg_owner")
    return bot


async def test_escalation_in_a_group_mentions_the_responsible_person_in_the_thread(database, httpx_mock, model):
    bot = await harness(database, httpx_mock, model)
    model.replies += escalating()
    asked = bot.telegram.message(GROUP, ANNA, "@assistant_test_bot client@example.com charged, nothing came")
    await bot.step()
    call = next(body for body in bot.telegram.sent_messages() if "A responsible person is needed." in body["text"])
    assert call["chat_id"] == GROUP and call["reply_parameters"]["message_id"] == asked
    assert {"type": "text_mention", "offset": 0, "length": 4, "user": {"id": OLEG}} in call["entities"]
    for part in ("Complaint", "Checked", "Found", "Question", "client@example.com", "Refund or rerun?"):
        assert part in call["text"]


async def test_escalation_of_a_private_case_goes_to_the_responsible_person_privately(database, httpx_mock, model):
    bot = await harness(database, httpx_mock, model)
    model.replies += escalating()
    bot.telegram.private(ANNA, "client@example.com charged, nothing came")
    await bot.step()
    to_owner = bot.telegram.texts(OLEG)
    assert len(to_owner) == 1 and "Asked by anna." in to_owner[0]


async def test_nobody_to_call_is_told_and_journaled(database, httpx_mock, model):
    bot = await harness(database, httpx_mock, model, owners=False)
    model.replies += escalating()
    bot.telegram.message(GROUP, ANNA, "@assistant_test_bot client@example.com charged, nothing came")
    await bot.step()
    assert any("There is nobody to call" in text for text in bot.telegram.texts(GROUP))
    async with database.unit_of_work() as session:
        entry = await session.scalar(select(JournalEntry).where(JournalEntry.kind == JournalKind.ESCALATED))
    assert entry.details["nobody"] is True


async def test_five_complaints_of_one_failure_call_once(database, httpx_mock, model):
    bot = await harness(database, httpx_mock, model)
    for index in range(5):
        model.replies += escalating("results lost after charge")
        bot.telegram.message(GROUP, ANNA, f"@assistant_test_bot client{index}@example.com charged, nothing came")
        await bot.step()
    calls = [text for text in bot.telegram.texts(GROUP) if "A responsible person is needed." in text]
    assert len(calls) == 1
    refusals = [
        result
        for request in model.requests
        for message in request["messages"]
        if message["role"] == "user" and isinstance(message["content"], list)
        for result in message["content"]
        if result.get("type") == "tool_result" and result.get("is_error")
    ]
    assert len(refusals) == 4 and "do not call again" in str(refusals[0])
    async with database.unit_of_work() as session:
        incident = await session.scalar(select(Incident))
    assert len(list(await database_cases(database, incident.id))) == 5


async def database_cases(database, incident_id):
    from internal.entity import Case

    async with database.unit_of_work() as session:
        return list(await session.scalars(select(Case).where(Case.incident_id == incident_id)))


async def test_rule_is_offered_to_the_responsible_person_and_instructions_stay(database, httpx_mock, model):
    bot = await harness(database, httpx_mock, model)
    model.replies += escalating()
    asked = bot.telegram.message(GROUP, ANNA, "@assistant_test_bot refund client@example.com?")
    await bot.step()
    model.replies += [
        reply(tool_use("propose_rule", {"rule": "A charge without a result is refunded in full."})),
        reply(tool_use("submit_answer", {"kind": "reply", "text": "done", "attachments": [], **NO_PARTS})),
        reply({"type": "text", "text": ""}, stop_reason="end_turn"),
    ]
    bot.telegram.message(GROUP, OLEG, "refund in full", reply_to_message=bot.telegram.in_thread(asked))
    await bot.step()
    assert "A charge without a result is refunded in full." in bot.telegram.texts(OLEG)[-1]


async def test_a_case_does_not_wait_for_a_responsible_person_nobody_called(database, httpx_mock, model):
    from internal.entity import Case, CaseState

    bot = await harness(database, httpx_mock, model, owners=False)
    model.replies += escalating()
    bot.telegram.message(GROUP, ANNA, "@assistant_test_bot refund client@example.com?")
    await bot.step()
    async with database.unit_of_work() as session:
        assert (await session.scalar(select(Case))).state is CaseState.INVESTIGATING
    assert "waiting_owner is only for a case a call went out for" in str(model.requests)


async def test_a_responsible_person_who_asks_is_not_called_the_others_are(database, httpx_mock, model):
    bot = await harness(database, httpx_mock, model)
    bot.provider.account("sub-ivan", "ivan", ["support", "owner", "escalation"])
    await bot.bind(504, "sub-ivan", "ivan_owner")
    model.replies += escalating()
    bot.telegram.private(OLEG, "client@example.com charged, nothing came")
    await bot.step()
    assert not any("A responsible person is needed." in text for text in bot.telegram.texts(OLEG))
    to_other = bot.telegram.texts(504)
    assert len(to_other) == 1 and "Asked by oleg." in to_other[0]


async def test_the_only_responsible_person_asking_is_told_there_is_nobody_else(database, httpx_mock, model):
    bot = await harness(database, httpx_mock, model)
    model.replies += escalating()
    bot.telegram.private(OLEG, "client@example.com charged, nothing came")
    await bot.step()
    texts = bot.telegram.texts(OLEG)
    assert not any("A responsible person is needed." in text for text in texts)
    assert any("you are the only responsible person" in text for text in texts)


async def test_an_owner_without_the_escalation_role_is_not_called(database, httpx_mock, model):
    bot = await harness(database, httpx_mock, model)
    bot.provider.account("sub-olga", "olga", ["support", "owner"])
    await bot.bind(505, "sub-olga", "olga_owner")
    model.replies += escalating()
    bot.telegram.private(ANNA, "client@example.com charged, nothing came")
    await bot.step()
    assert bot.telegram.texts(505) == []
    assert len(bot.telegram.texts(OLEG)) == 1


async def test_the_escalation_role_alone_is_called_and_takes_the_thread(database, httpx_mock, model):
    from internal.entity import Case

    bot = await harness(database, httpx_mock, model, owners=False)
    bot.provider.account("sub-ivan", "ivan", ["support", "escalation"])
    await bot.bind(504, "sub-ivan", "ivan_esc")
    model.replies += escalating()
    asked = bot.telegram.message(GROUP, ANNA, "@assistant_test_bot client@example.com charged, nothing came")
    await bot.step()
    call = next(body for body in bot.telegram.sent_messages() if "A responsible person is needed." in body["text"])
    assert {"type": "text_mention", "offset": 0, "length": 4, "user": {"id": 504}} in call["entities"]
    model.replies += [
        reply(tool_use("submit_answer", {"kind": "reply", "text": "noted", "attachments": [], **NO_PARTS})),
        reply({"type": "text", "text": ""}, stop_reason="end_turn"),
    ]
    bot.telegram.message(GROUP, 504, "refund in full", reply_to_message=bot.telegram.in_thread(asked))
    await bot.step()
    async with database.unit_of_work() as session:
        case = await session.scalar(select(Case))
    assert case.owner_took_over is True
