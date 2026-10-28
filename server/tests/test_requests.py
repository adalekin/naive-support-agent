import datetime
import json

import pytest
from sqlalchemy import select, update

from internal.bot.outbox import Outbox
from internal.entity import Case, CaseState, JournalEntry, JournalKind
from tests.fake_model import FakeModel, reply, tool_use
from tests.harness import GROUP, Harness

ANNA, OLEG, EVE, STRANGER = 501, 502, 503, 999
NO_PARTS = dict.fromkeys(["client_saw", "cause", "whose_error", "tell_client_now", "next_steps"])


def answer(text, state="resolved", waiting_for=None):
    return [
        reply(tool_use("set_case_state", {"state": state, "waiting_for": waiting_for})),
        reply(tool_use("submit_answer", {"kind": "reply", "text": text, "attachments": [], **NO_PARTS})),
        reply({"type": "text", "text": ""}, stop_reason="end_turn"),
    ]


def classified(is_case):
    return reply({"type": "text", "text": f'{{"is_case": {"true" if is_case else "false"}}}'}, stop_reason="end_turn")


@pytest.fixture
def model():
    return FakeModel()


@pytest.fixture
async def bot(database, httpx_mock, model):
    harness = await Harness(database, httpx_mock, model).start()
    await harness.bind(ANNA, "sub-anna", "anna_s")
    await harness.bind(OLEG, "sub-oleg", "oleg_owner")
    await harness.bind(EVE, "sub-eve", "eve_x")
    return harness


async def journal(database, kind):
    async with database.unit_of_work() as session:
        return list(await session.scalars(select(JournalEntry).where(JournalEntry.kind == kind)))


# staff-access


async def test_staff_with_the_role_gets_the_case_taken_and_answered(bot, model, database):
    model.replies += answer("The payment of <<email_1>> was declined by the bank.")
    asked = bot.telegram.message(GROUP, ANNA, "@assistant_test_bot client@example.com cannot pay")
    await bot.step()
    sent = bot.telegram.sent_messages()
    assert sent[0]["text"] == "On it." and sent[0]["reply_parameters"]["message_id"] == asked
    assert sent[1]["text"] == "The payment of client@example.com was declined by the bank."
    assert sent[1]["entities"] == [{"type": "spoiler", "offset": 15, "length": 18}]
    assert sent[1]["reply_parameters"]["message_id"] == asked
    assert "inline_keyboard" in sent[1]["reply_markup"]
    entries = await journal(database, JournalKind.REQUEST)
    assert [entry.subject for entry in entries] == ["sub-anna"]


async def test_bound_person_without_the_role_gets_nothing(bot, model, database):
    bot.telegram.message(GROUP, EVE, "@assistant_test_bot client@example.com cannot pay")
    await bot.step()
    assert bot.telegram.sent == [] and model.requests == []
    refused = await journal(database, JournalKind.REFUSED)
    assert [(entry.telegram_user_id, entry.chat_id, entry.details) for entry in refused] == [
        (EVE, GROUP, {"reason": "no_role"})
    ]


async def test_stranger_in_the_group_gets_no_answer_and_no_link(bot, database):
    bot.telegram.message(GROUP, STRANGER, "@assistant_test_bot what is the email of client 7?")
    await bot.step()
    assert bot.telegram.sent == []
    refused = await journal(database, JournalKind.REFUSED)
    assert refused[0].telegram_user_id == STRANGER
    assert "email" not in str(refused[0].details)


async def test_stranger_in_a_private_chat_gets_only_the_sign_in_link(bot, model):
    bot.telegram.private(STRANGER, "client@example.com cannot pay")
    await bot.step()
    assert len(bot.telegram.texts()) == 1 and "USER-" in bot.telegram.texts()[0]
    assert model.requests == []


async def test_anonymous_administrator_gets_nothing(bot, database, model):
    bot.telegram.message(GROUP, 1087968824, "@assistant_test_bot help", sender_chat={"id": GROUP, "type": "supergroup"})
    await bot.step()
    assert bot.telegram.sent == [] and model.requests == []
    assert (await journal(database, JournalKind.REFUSED))[0].details == {"reason": "on_behalf_of_chat"}


async def test_forwarded_by_a_stranger_is_not_taken(bot, model):
    bot.telegram.private(
        STRANGER, "staff wrote this", forward_origin={"type": "user", "date": 1, "sender_user": {"id": ANNA}}
    )
    await bot.step()
    assert model.requests == []


async def test_forwarded_by_staff_is_taken(bot, model):
    model.replies += answer("Seen.")
    bot.telegram.private(
        ANNA, "my payment failed", forward_origin={"type": "hidden_user", "date": 1, "sender_user_name": "Client"}
    )
    await bot.step()
    assert bot.telegram.texts(ANNA) == ["On it.", "Seen."]
    assert "forwarded from a client" in model.requests[0]["messages"][0]["content"][0]["text"]


async def test_stranger_with_the_user_name_of_staff_gets_nothing(bot, model):
    bot.telegram.message(GROUP, STRANGER, "@assistant_test_bot hi", sender_extra={"username": "anna_s"})
    await bot.step()
    assert bot.telegram.sent == [] and model.requests == []


async def test_staff_who_changed_the_user_name_is_still_answered(bot, model):
    model.replies += answer("Fine.")
    bot.telegram.message(GROUP, ANNA, "@assistant_test_bot ok?", sender_extra={"username": "anna_new"})
    await bot.step()
    assert bot.telegram.texts(GROUP) == ["On it.", "Fine."]


async def test_bot_leaves_a_group_not_on_the_list(bot, model):
    bot.telegram.added_to(-5)
    bot.telegram.message(-6, ANNA, "@assistant_test_bot help")
    await bot.step()
    assert [(method, body["chat_id"]) for method, body in bot.telegram.sent] == [("leaveChat", -5), ("leaveChat", -6)]
    assert model.requests == []


async def test_provider_down_means_no_investigation_and_a_message(bot, model):
    bot.provider.down = True
    bot.telegram.message(GROUP, ANNA, "@assistant_test_bot client@example.com cannot pay")
    await bot.step()
    assert model.requests == []
    assert "cannot check your access" in bot.telegram.texts(GROUP)[0]


async def test_role_removed_then_a_reply_in_yesterdays_thread_gets_nothing(bot, model):
    model.replies += answer("Answer.")
    bot.telegram.message(GROUP, ANNA, "@assistant_test_bot client@example.com cannot pay")
    await bot.step()
    answer_id = bot.telegram.number_of("Answer.")
    bot.provider.accounts["sub-anna"]["roles"] = []
    sent_before = len(bot.telegram.sent)
    bot.telegram.message(
        GROUP,
        ANNA,
        "and yesterday?",
        reply_to_message={"message_id": answer_id, "chat": {"id": GROUP, "type": "supergroup"}, "date": 1},
    )
    await bot.step()
    assert len(bot.telegram.sent) == sent_before


# case-requests


async def test_group_talk_without_a_mention_is_ignored_without_trace(bot, model, database):
    bot.telegram.message(GROUP, ANNA, "lunch at one?")
    bot.telegram.message(GROUP, STRANGER, "anyone?")
    await bot.step()
    assert bot.telegram.sent == [] and model.requests == []
    assert await journal(database, JournalKind.REFUSED) == []


async def test_a_reply_in_the_thread_continues_the_same_case(bot, model, database):
    model.replies += answer("First answer about <<email_1>>.", state="waiting_client")
    asked = bot.telegram.message(GROUP, ANNA, "@assistant_test_bot client@example.com cannot pay")
    await bot.step()
    answer_id = next(
        number
        for number, body in enumerate(bot.telegram.sent_messages(), start=1001)
        if body["text"].startswith("First")
    )
    model.replies += answer("Twice this week.")
    bot.telegram.message(
        GROUP,
        ANNA,
        "how many times this week?",
        reply_to_message={"message_id": answer_id, "chat": {"id": GROUP, "type": "supergroup"}, "date": 1},
    )
    await bot.step()
    async with database.unit_of_work() as session:
        cases = list(await session.scalars(select(Case)))
    assert len(cases) == 1 and cases[0].root_message_id == asked
    second_turn = model.requests[3]["messages"]
    assert second_turn[0]["content"][0]["text"].endswith("<<email_1>> cannot pay")
    assert bot.telegram.texts(GROUP)[-1] == "Twice this week."


async def test_two_cases_each_answered_under_its_own_message(bot, model):
    model.replies += answer("About one.") + answer("About two.")
    first = bot.telegram.message(GROUP, ANNA, "@assistant_test_bot case one")
    second = bot.telegram.message(GROUP, ANNA, "@assistant_test_bot case two")
    await bot.step()
    answers = {
        body["reply_parameters"]["message_id"]
        for body in bot.telegram.sent_messages()
        if body["text"].startswith("About")
    }
    assert answers == {first, second}


async def test_responsible_person_called_not_about_a_case_gets_nothing(bot, model, database):
    model.replies.append(classified(False))
    bot.telegram.message(GROUP, ANNA, "@oleg_owner when is the call?")
    await bot.step()
    assert bot.telegram.sent == []
    async with database.unit_of_work() as session:
        assert list(await session.scalars(select(Case))) == []


async def test_responsible_person_called_about_a_case_the_assistant_takes_it(bot, model):
    model.replies += [classified(True), *answer("Declined by the bank.")]
    bot.telegram.message(GROUP, ANNA, "@oleg_owner client@example.com payment failed")
    await bot.step()
    assert bot.telegram.texts(GROUP) == ["On it.", "Declined by the bank."]


async def test_responsible_person_answered_in_the_thread_the_assistant_stops(bot, model, database):
    model.replies += answer("Looking.", state="waiting_owner")
    asked = bot.telegram.message(GROUP, ANNA, "@assistant_test_bot refund for client@example.com?")
    await bot.step()
    model.replies += [
        reply(tool_use("propose_rule", {"rule": "Refund in full when the bank declined after the charge."})),
        reply(tool_use("submit_answer", {"kind": "reply", "text": "done", "attachments": [], **NO_PARTS})),
        reply({"type": "text", "text": ""}, stop_reason="end_turn"),
    ]
    in_thread = {"message_id": asked, "chat": {"id": GROUP, "type": "supergroup"}, "date": 1}
    bot.telegram.message(GROUP, OLEG, "refund in full", reply_to_message=in_thread)
    await bot.step()
    assert bot.telegram.texts(GROUP) == ["On it.", "Looking."]
    assert "Refund in full" in bot.telegram.texts(OLEG)[0]
    requests_before = len(model.requests)
    bot.telegram.message(GROUP, ANNA, "thanks", reply_to_message=in_thread)
    await bot.step()
    assert len(model.requests) == requests_before
    assert bot.telegram.texts(GROUP) == ["On it.", "Looking."]


# telegram


async def test_an_update_is_not_handled_twice_after_a_restart(bot, model):
    model.replies += answer("Once.")
    bot.telegram.message(GROUP, ANNA, "@assistant_test_bot x")
    await bot.step()
    # A new listener over the same database: the position of the updates is kept there.
    await bot.step()
    assert bot.telegram.texts(GROUP) == ["On it.", "Once."]


# case-followup


async def test_answer_rated_not_helpful_asks_what_is_wrong_and_looks_again(bot, model):
    model.replies += answer("Bank declined.")
    bot.telegram.message(GROUP, ANNA, "@assistant_test_bot client@example.com cannot pay")
    await bot.step()
    answer_id = bot.telegram.number_of("Bank declined.")
    bot.telegram.press(ANNA, GROUP, answer_id, "f:1:down")
    await bot.step()
    question = bot.telegram.texts(GROUP)[-1]
    assert question.startswith("What is wrong")
    question_id = bot.telegram.number_of("What is wrong")
    model.replies += answer("The card was fine; the limit was hit.")
    bot.telegram.message(
        GROUP,
        ANNA,
        "the card is fine",
        reply_to_message={"message_id": question_id, "chat": {"id": GROUP, "type": "supergroup"}, "date": 1},
    )
    await bot.step()
    assert "not helpful" in str(model.requests[-3]["messages"])
    assert bot.telegram.texts(GROUP)[-1] == "The card was fine; the limit was hit."


async def test_only_the_person_who_asked_rates_the_answer(bot, model, database):
    model.replies += answer("x")
    bot.telegram.message(GROUP, ANNA, "@assistant_test_bot y")
    await bot.step()
    answer_id = bot.telegram.number_of("x")
    bot.telegram.press(OLEG, GROUP, answer_id, "f:1:up")
    await bot.step()
    assert (
        "answerCallbackQuery",
        {"callback_query_id": f"q{answer_id}", "text": "Only the person who asked can rate the answer."},
    ) in bot.telegram.sent
    assert await journal(database, JournalKind.FEEDBACK) == []


async def test_fix_released_is_told_in_the_thread(bot, model, database):
    model.replies += answer(
        "A bug; waiting for the fix.", state="waiting_fix", waiting_for="the fix of payment retries"
    )
    bot.telegram.message(GROUP, ANNA, "@assistant_test_bot client@example.com charged twice")
    await bot.step()
    model.replies += answer("Not yet.", state="waiting_fix", waiting_for="the fix of payment retries")
    assert await bot.follow_up.tick() == 1
    await bot.worker.drain_all()
    assert bot.telegram.texts(GROUP)[-1] == "A bug; waiting for the fix."
    model.replies += answer("The fix is available to clients; the client can be told.")
    await bot.follow_up.tick()
    await bot.worker.drain_all()
    assert bot.telegram.texts(GROUP)[-1] == "The fix is available to clients; the client can be told."
    async with database.unit_of_work() as session:
        assert (await session.scalar(select(Case))).state is CaseState.RESOLVED


async def test_responsible_person_bound_through_sign_in_is_recognised_when_mentioned(database, httpx_mock, model):
    bot = await Harness(database, httpx_mock, model).start()
    await bot.bind(ANNA, "sub-anna", "anna_s")
    bot.telegram.private(OLEG, "hello", sender_extra={"username": "oleg_owner"})
    await bot.step()
    bot.provider.confirm(next(iter(bot.provider.devices)), "sub-oleg")
    async with database.unit_of_work() as session:
        later = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=10)
        await bot.login.poll_due(session, Outbox(), later)
    model.replies += [classified(True), *answer("Declined by the bank.")]
    bot.telegram.message(GROUP, ANNA, "@oleg_owner client@example.com payment failed")
    await bot.step()
    assert bot.telegram.texts(GROUP)[-2:] == ["On it.", "Declined by the bank."]


async def test_an_update_that_fails_is_skipped_and_the_next_one_is_handled(bot, model, monkeypatch):
    from internal.bot.handlers import HandleMembership

    async def broken(self, event, desk):
        raise RuntimeError("bug")

    monkeypatch.setattr(HandleMembership, "handle", broken)
    model.replies += answer("Still here.")
    bot.telegram.added_to(GROUP)
    bot.telegram.message(GROUP, ANNA, "@assistant_test_bot x")
    await bot.step()
    await bot.step()
    assert bot.telegram.texts(GROUP) == ["On it.", "Still here."]


async def test_a_case_that_waits_for_a_fix_is_kept_by_its_checks(bot, model, database):
    from internal.bot.retention import RetentionSweeper

    model.replies += answer("Waiting for the fix.", state="waiting_fix", waiting_for="the retry fix")
    bot.telegram.message(GROUP, ANNA, "@assistant_test_bot client@example.com charged twice")
    await bot.step()
    async with database.unit_of_work() as session:
        await session.execute(
            update(Case).values(updated_at=datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=40))
        )
    model.replies += answer("Not yet.", state="waiting_fix", waiting_for="the retry fix")
    await bot.follow_up.tick()
    await bot.worker.drain_all()
    assert await RetentionSweeper(database, 30).sweep(datetime.datetime.now(datetime.UTC)) == 0


async def test_only_a_person_with_the_role_is_told_aggregate_figures_are_allowed(database, httpx_mock, model):
    from internal.bot.worker import ANALYTICS_NOTE

    bot = await Harness(database, httpx_mock, model).start()
    bot.provider.account("sub-boss", "boss", ["support"], admin_roles=["admin"])
    await bot.bind(ANNA, "sub-anna", "anna_s")
    await bot.bind(600, "sub-boss", "boss")
    model.replies += answer("Refused.") + answer("990 today.")
    bot.telegram.private(ANNA, "what is the revenue today?")
    await bot.step()
    bot.telegram.private(600, "what is the revenue today?")
    await bot.step()
    first_of_anna, first_of_boss = model.requests[0], model.requests[3]
    assert ANALYTICS_NOTE not in json.dumps(first_of_anna)
    assert ANALYTICS_NOTE in json.dumps(first_of_boss)
