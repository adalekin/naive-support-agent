"""What the assistant does with what its bot is told: one handler for each kind of update.

``UpdateDispatcher`` finds the handler by the kind of the event and knows no kind itself. A
handler works inside the unit of work of its update and leaves what to say in the outbox: it is
sent after the transaction commits. Investigating a case takes minutes, so a handler only
accepts it, as a turn kept in the database, and the worker takes it from there.

Who may get anything from the assistant is decided here, before anything is kept: the group is
on the installation's list, the message has a person behind it, the person is bound and the
provider says they hold the role now. A message nobody addressed to the assistant is dropped
without a trace; one addressed to it by someone without access is journaled by numbers only.
"""

import dataclasses
import datetime
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from loguru import logger
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from internal.access.guard import AccessGuard, Staff
from internal.access.login import DeviceLoginProcess
from internal.access.oidc import ProviderUnavailable
from internal.bot.outbox import AnswerPress, ChangeKeyboard, Leave, Outbox, Send
from internal.entity import Case, JournalKind, MessageRole, StaffBinding, TurnKind
from internal.repository import CaseRepository, Journal, StaffRepository
from internal.telegram import Attachment, Event, MembershipChanged, Pressed, Said, Text
from internal.words import Words

GROUP_CHATS = ("group", "supergroup")
#: Statuses of the bot in a chat that mean it is there.
PRESENT = ("member", "administrator", "restricted")


@dataclass
class Desk:
    """What a handler works with: the transaction of the update, the time, and the outbox."""

    session: AsyncSession
    now: datetime.datetime
    outbox: Outbox


@dataclass(frozen=True)
class Policy:
    """The installation's answers to who and where."""

    bot_username: str
    groups: frozenset[int]


class Handler[E: Event](ABC):
    @abstractmethod
    async def handle(self, event: E, desk: Desk) -> None: ...


def turn_payload(event: Said, staff: Staff) -> dict[str, Any]:
    def attachments(items: tuple[Attachment, ...]) -> list[dict[str, Any]]:
        return [dataclasses.asdict(item) for item in items]

    quoted = None
    if event.reply_to is not None:
        quoted = {"text": event.reply_to.text, "attachments": attachments(event.reply_to.attachments)}
    return {
        "staff_telegram_id": staff.binding.telegram_user_id,
        "staff_subject": staff.identity.subject,
        "staff_account": staff.identity.account_name,
        "chat_id": event.chat_id,
        "message_id": event.message_id,
        "thread_id": event.thread_id,
        "text": event.text,
        "forwarded": event.forwarded,
        "attachments": attachments(event.attachments),
        "quoted": quoted,
    }


class HandleMessage(Handler[Said]):
    def __init__(self, policy: Policy, guard: AccessGuard, login: DeviceLoginProcess, words: Words) -> None:
        self._policy = policy
        self._guard = guard
        self._login = login
        self._words = words

    async def handle(self, event: Said, desk: Desk) -> None:
        if event.sender_is_bot and not event.on_behalf_of_chat:
            return
        if event.chat_type == "private":
            await self._private(event, desk)
        elif event.chat_type in GROUP_CHATS:
            await self._group(event, desk)

    async def _private(self, event: Said, desk: Desk) -> None:
        if event.sender_id is None:
            return
        staff = await self._staff(event, desk, reply_in_chat=True)
        if staff is None:
            bound = await StaffRepository(desk.session).by_telegram(event.sender_id)
            if bound is None:
                await self._refused(event, desk, "not_bound")
                await self._login.on_private_message(
                    desk.session, desk.outbox, event.sender_id, event.chat_id, event.sender_username, desk.now
                )
            return
        await self._accept(event, desk, staff, owner_mention=False)

    async def _group(self, event: Said, desk: Desk) -> None:
        if event.chat_id not in self._policy.groups:
            desk.outbox.add(Leave(event.chat_id))
            return

        cases = CaseRepository(desk.session)
        thread = await cases.of_message(event.chat_id, event.reply_to.message_id) if event.reply_to else None
        to_assistant = self._policy.bot_username.lower() in event.mentioned_usernames
        mentioned = await self._mentioned_bindings(event, desk) if not to_assistant and thread is None else []
        if not (to_assistant or thread is not None or mentioned):
            # Ordinary talk of the group: not kept, not journaled.
            return
        if event.on_behalf_of_chat or event.sender_id is None:
            if to_assistant or thread is not None:
                await self._refused(event, desk, "on_behalf_of_chat")
            return

        if thread is not None and not to_assistant:
            await self._in_thread(event, desk, thread)
            return
        if to_assistant:
            staff = await self._staff(event, desk, reply_in_chat=False)
            if staff is not None:
                await self._accept(event, desk, staff, owner_mention=False)
            return

        # The staff called someone by name: a case only if a responsible person was called about one.
        try:
            called = [staff for binding in mentioned if (staff := await self._guard.check(binding))]
        except ProviderUnavailable:
            logger.warning("Could not tell whether a responsible person was called: the provider is unavailable")
            return
        if not any(staff.takes_escalations for staff in called):
            return
        staff = await self._staff(event, desk, reply_in_chat=False)
        if staff is not None:
            await self._accept(event, desk, staff, owner_mention=True)

    async def _in_thread(self, event: Said, desk: Desk, case: Case, staff: Staff | None = None) -> None:
        if case.owner_took_over:
            return
        staff = staff or await self._staff(event, desk, reply_in_chat=False)
        if staff is None:
            return
        cases = CaseRepository(desk.session)
        if staff.takes_escalations and staff.identity.subject != case.staff_subject:
            # The responsible person answered: the assistant stops writing in the thread and offers
            # them a rule for the instructions instead.
            case.owner_took_over = True
            await cases.remember_message(event.chat_id, event.message_id, case.id, MessageRole.REQUEST)
            await cases.enqueue(case, TurnKind.OWNER_ANSWER, turn_payload(event, staff))
            return
        replied = await cases.message(event.chat_id, event.reply_to.message_id) if event.reply_to else None
        kind = (
            TurnKind.RETRY
            if replied is not None and replied.role is MessageRole.FEEDBACK_QUESTION
            else TurnKind.REQUEST
        )
        await cases.remember_message(event.chat_id, event.message_id, case.id, MessageRole.REQUEST)
        await cases.enqueue(case, kind, turn_payload(event, staff))

    async def _accept(self, event: Said, desk: Desk, staff: Staff, *, owner_mention: bool) -> None:
        cases = CaseRepository(desk.session)
        # A reply in a private chat to a message of a case continues that case.
        if event.chat_type == "private" and event.reply_to is not None:
            case = await cases.of_message(event.chat_id, event.reply_to.message_id)
            if case is not None:
                await self._in_thread(event, desk, case, staff)
                return

        case = await cases.open(event.chat_id, event.message_id, event.chat_type == "private", staff.identity.subject)
        kind = TurnKind.OWNER_MENTION if owner_mention else TurnKind.REQUEST
        await cases.enqueue(case, kind, turn_payload(event, staff))
        if not owner_mention:
            # Said before the model is called: an investigation takes minutes.
            desk.outbox.add(
                Send(
                    event.chat_id,
                    Text(self._words.say("taken")),
                    reply_to=event.message_id,
                    thread_id=event.thread_id,
                    case_id=case.id,
                )
            )

    async def _staff(self, event: Said, desk: Desk, *, reply_in_chat: bool) -> Staff | None:
        """The sender as a member of staff who may use the assistant now, or None."""
        assert event.sender_id is not None
        try:
            staff = await self._guard.check(event.sender_id)
        except ProviderUnavailable:
            bound = await StaffRepository(desk.session).by_telegram(event.sender_id)
            if bound is not None:
                desk.outbox.add(
                    Send(
                        event.chat_id,
                        Text(self._words.say("access_unavailable")),
                        reply_to=event.message_id,
                        thread_id=event.thread_id,
                    )
                )
            return None
        if staff is not None and staff.may_use:
            # Kept for recognising a mention of this person, never for deciding who they are.
            if event.sender_username != staff.binding.telegram_username:
                await desk.session.execute(
                    update(StaffBinding)
                    .where(StaffBinding.telegram_user_id == staff.binding.telegram_user_id)
                    .values(telegram_username=event.sender_username)
                )
            return staff
        if staff is not None or not reply_in_chat:
            await self._refused(event, desk, "no_role" if staff is not None else "not_bound")
        return None

    async def _mentioned_bindings(self, event: Said, desk: Desk) -> list[int]:
        staff = StaffRepository(desk.session)
        found = []
        for username in event.mentioned_usernames:
            binding = await staff.by_username(username)
            if binding is not None:
                found.append(binding.telegram_user_id)
        for telegram_user_id in event.mentioned_ids:
            if await staff.by_telegram(telegram_user_id) is not None:
                found.append(telegram_user_id)
        return found

    @staticmethod
    async def _refused(event: Said, desk: Desk, reason: str) -> None:
        # Numbers only: the text of someone without access is not kept.
        await Journal(desk.session).write(
            JournalKind.REFUSED, telegram_user_id=event.sender_id, chat_id=event.chat_id, reason=reason
        )


class HandlePress(Handler[Pressed]):
    """A rating of an answer: the buttons under it."""

    def __init__(self, guard: AccessGuard, words: Words) -> None:
        self._guard = guard
        self._words = words

    async def handle(self, event: Pressed, desk: Desk) -> None:
        kind, _, rest = event.data.partition(":")
        case_id, _, verdict = rest.partition(":")
        case = await CaseRepository(desk.session).get(int(case_id)) if kind == "f" and case_id.isdigit() else None
        if case is None or verdict not in ("up", "down"):
            desk.outbox.add(AnswerPress(event.press_id, self._words.say("button_gone")))
            return
        try:
            staff = await self._guard.check(event.sender_id)
        except ProviderUnavailable:
            desk.outbox.add(AnswerPress(event.press_id, self._words.say("access_unavailable")))
            return
        if staff is None or not staff.may_use or staff.identity.subject != case.staff_subject:
            desk.outbox.add(AnswerPress(event.press_id, self._words.say("not_yours")))
            return

        await Journal(desk.session).write(
            JournalKind.FEEDBACK,
            subject=staff.identity.subject,
            case_id=case.id,
            client_account=case.client_account,
            helpful=verdict == "up",
        )
        desk.outbox.add(AnswerPress(event.press_id, self._words.say("rated")))
        desk.outbox.add(ChangeKeyboard(event.chat_id, event.message_id, None))
        if verdict == "down":
            desk.outbox.add(
                Send(
                    event.chat_id,
                    Text(self._words.say("what_is_wrong")),
                    reply_to=event.message_id,
                    case_id=case.id,
                    role=MessageRole.FEEDBACK_QUESTION,
                )
            )


class HandleMembership(Handler[MembershipChanged]):
    """The bot leaves every chat that is not on the installation's list, as soon as it is added."""

    def __init__(self, policy: Policy) -> None:
        self._policy = policy

    async def handle(self, event: MembershipChanged, desk: Desk) -> None:
        if event.chat_type == "private" or event.status not in PRESENT:
            return
        if event.chat_type == "channel" or event.chat_id not in self._policy.groups:
            logger.info("The bot was added to a chat not on the list and leaves it")
            desk.outbox.add(Leave(event.chat_id))


class UpdateDispatcher:
    def __init__(self, handlers: dict[type, Handler]) -> None:
        self._handlers = handlers

    async def dispatch(self, event: Event | None, desk: Desk) -> None:
        if event is None:
            return
        handler = self._handlers.get(type(event))
        if handler is not None:
            await handler.handle(event, desk)
