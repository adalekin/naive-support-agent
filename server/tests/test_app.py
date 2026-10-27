import asyncio

import pytest
from sqlalchemy import text

from internal.app import Assistant
from internal.config import load_settings
from tests.harness import INSTRUCTIONS


async def test_assistant_waits_for_the_migrations(environment, database):
    assistant = Assistant(load_settings(), INSTRUCTIONS)
    await asyncio.wait_for(assistant.wait_for_schema(), 5)
    async with database.engine.begin() as connection:
        await connection.execute(text("UPDATE alembic_version SET version_num = 'older'"))
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(assistant.wait_for_schema(), 1)
    finally:
        async with database.engine.begin() as connection:
            await connection.execute(text("UPDATE alembic_version SET version_num = '0001'"))
        await assistant.database.dispose()
