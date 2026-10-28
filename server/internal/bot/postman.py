from loguru import logger

from internal.bot.outbox import AnswerPress, ChangeKeyboard, Leave, Outbox, Send, SendMedia
from internal.db import Database
from internal.repository import CaseRepository
from internal.telegram import ChatClosed, RichMessage, TelegramGateway, TelegramUnavailable, Text


class Postman:
    """Sends what an event left in the outbox, after its transaction committed.

    A message of a case is remembered as part of it, so that a reply to it continues the case.
    """

    def __init__(self, telegram: TelegramGateway, database: Database) -> None:
        self._telegram = telegram
        self._database = database

    async def deliver(self, outbox: Outbox) -> None:
        for reply in outbox.replies:
            try:
                match reply:
                    case Send(text=RichMessage() as rich):
                        number = await self._telegram.send_rich(
                            reply.chat_id, rich, reply_to=reply.reply_to, thread_id=reply.thread_id,
                            keyboard=reply.keyboard,
                        )  # fmt: skip
                        await self._remember(reply.chat_id, number, reply.case_id, reply)
                    case Send(text=Text() as text):
                        numbers = await self._telegram.send_message(
                            reply.chat_id, text, reply_to=reply.reply_to, thread_id=reply.thread_id,
                            keyboard=reply.keyboard,
                        )  # fmt: skip
                        for number in numbers:
                            await self._remember(reply.chat_id, number, reply.case_id, reply)
                    case SendMedia():
                        number = await self._telegram.send_media(
                            reply.chat_id, reply.kind, reply.content, reply.file_name, caption=reply.caption,
                            reply_to=reply.reply_to, thread_id=reply.thread_id,
                        )  # fmt: skip
                        await self._remember(reply.chat_id, number, reply.case_id, reply)
                    case AnswerPress():
                        await self._telegram.answer_press(reply.press_id, reply.text)
                    case ChangeKeyboard():
                        await self._telegram.set_keyboard(reply.chat_id, reply.message_id, reply.keyboard)
                    case Leave():
                        await self._telegram.leave_chat(reply.chat_id)
            except (TelegramUnavailable, ChatClosed) as exc:
                # What the event did is committed; the message is lost, not the work.
                logger.warning("A message of the assistant was not delivered: {}", type(exc).__name__)

    async def _remember(self, chat_id: int, message_id: int, case_id: int | None, reply: Send | SendMedia) -> None:
        if case_id is None:
            return
        async with self._database.unit_of_work() as session:
            await CaseRepository(session).remember_message(chat_id, message_id, case_id, reply.role)
