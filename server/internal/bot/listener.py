import datetime

from loguru import logger

from internal.bot.handlers import Desk, UpdateDispatcher
from internal.bot.outbox import Outbox
from internal.bot.postman import Postman
from internal.db import Database
from internal.repository import OffsetRepository
from internal.telegram import TelegramGateway

#: How long one call waits for the bot to be told something.
POLL_SECONDS = 25


class TelegramListener:
    """Reads the bot's updates and hands each to its handler, one transaction per update.

    The number of the next update moves in the same transaction as what the update did: after a
    restart no update is handled twice and none is skipped (an Idempotent Receiver).
    """

    def __init__(
        self, telegram: TelegramGateway, database: Database, dispatcher: UpdateDispatcher, postman: Postman
    ) -> None:
        self._telegram = telegram
        self._database = database
        self._dispatcher = dispatcher
        self._postman = postman

    async def read_once(self, wait_seconds: int = POLL_SECONDS) -> int:
        """One long poll. Returns how many updates were handled."""
        async with self._database.unit_of_work() as session:
            offset = await OffsetRepository(session).next_update_id()
        updates = await self._telegram.get_updates(offset, wait_seconds)
        for update in updates:
            outbox = Outbox()
            try:
                async with self._database.unit_of_work() as session:
                    if (await OffsetRepository(session).next_update_id() or 0) > update.id:
                        continue
                    desk = Desk(session=session, now=datetime.datetime.now(datetime.UTC), outbox=outbox)
                    await self._dispatcher.dispatch(update.event, desk)
                    await OffsetRepository(session).move_to(update.id + 1)
            except Exception:  # noqa: BLE001 - the top of the listener, see below
                # An update that fails would fail again after every restart and hold up all the
                # others: it is logged and passed over, nothing it did is kept.
                logger.exception("Update {} could not be handled and is skipped", update.id)
                async with self._database.unit_of_work() as session:
                    await OffsetRepository(session).move_to(update.id + 1)
                continue
            await self._postman.deliver(outbox)
        return len(updates)
