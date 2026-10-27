import asyncio
import datetime

from loguru import logger
from sqlalchemy import delete

from internal.db import Database
from internal.entity import Case, DeviceLogin

#: How often the sweep runs.
SWEEP_SECONDS = 3600


class RetentionSweeper:
    """Deletes the cases nobody worked on for longer than the installation keeps them.

    A case goes with its conversation, its labels, its thread and its turns; the journal keeps
    who asked about which client, with the link to the case emptied. A scheduled check is a turn
    too, so a case that waits for a fix stays.
    """

    def __init__(self, database: Database, retention_days: int) -> None:
        self._database = database
        self._retention = datetime.timedelta(days=retention_days)

    async def run_forever(self) -> None:
        while True:
            await self.sweep(datetime.datetime.now(datetime.UTC))
            await asyncio.sleep(SWEEP_SECONDS)

    async def sweep(self, now: datetime.datetime) -> int:
        async with self._database.unit_of_work() as session:
            deleted = await session.execute(
                delete(Case).where(Case.updated_at < now - self._retention).returning(Case.id)
            )
            count = len(deleted.all())
            await session.execute(delete(DeviceLogin).where(DeviceLogin.expires_at < now))
        if count:
            logger.info("{} cases past the retention period are deleted", count)
        return count
