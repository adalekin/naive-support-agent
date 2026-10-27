from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from internal.crypto import EncryptedJson, TokenCipher


class Database:
    """The assistant's own database. One transaction per event: ``unit_of_work``."""

    def __init__(self, url: URL | str, cipher: TokenCipher) -> None:
        # The data of cases is encrypted with the installation's key: one key per process.
        EncryptedJson.cipher = cipher
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def unit_of_work(self) -> AsyncIterator[AsyncSession]:
        """Everything done in the block is committed together, or nothing is."""
        async with self._sessions() as session, session.begin():
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
