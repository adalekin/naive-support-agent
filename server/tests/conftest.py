import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import create_async_engine

from internal.crypto import TokenCipher
from internal.db import Database
from internal.entity import Base

# Settings are fixed here, never read from a .env file: no test can reach a real bot,
# provider or model. The database is the test database of the development PostgreSQL
# from the repository's compose.yaml.
TEST_ENVIRONMENT = {
    "TELEGRAM_BOT_TOKEN": "1234:test-token",
    "TELEGRAM_BOT_USERNAME": "assistant_test_bot",
    "TELEGRAM_EGRESS": "direct",
    "TELEGRAM_PROXY_URL": "",
    "TELEGRAM_GROUPS": "-1001,-1002",
    "LOCALE": "en",
    "OIDC_ISSUER": "https://idp.test/realms/support",
    "OIDC_CLIENT_ID": "support-assistant",
    "OIDC_CLIENT_SECRET": "client-secret",
    "OIDC_ROLES_CLAIM": "resource_access.support-assistant.roles",
    "ROLE_SUPPORT": "support",
    "ROLE_OWNER": "owner",
    "ROLE_ANALYTICS": "admin",
    "ANALYTICS_ROLES_CLAIM": "resource_access.product-admin.roles",
    "ANTHROPIC_API_KEY": "test-model-key",
    "MODEL_EGRESS": "direct",
    "MODEL_PROXY_URL": "",
    "MODEL": "claude-opus-5-5",
    "MODEL_EFFORT": "high",
    "CASE_TOKEN_BUDGET": "200000",
    "INSTRUCTIONS_DIR": "/nonexistent",
    "ENCRYPTION_KEY": "8Kq7c5lW1m3hQ2oV0J9y6B4x8n2T5r1u7Z3d0f6g9aE=",
    "FOLLOWUP_INTERVAL_MINUTES": "60",
    "CASE_RETENTION_DAYS": "30",
    "DB_HOST": os.environ.get("TEST_DB_HOST", "127.0.0.1"),
    "DB_PORT": os.environ.get("TEST_DB_PORT", "54330"),
    "DB_USER": os.environ.get("TEST_DB_USER", "assistant"),
    "DB_PASSWORD": os.environ.get("TEST_DB_PASSWORD", "assistant"),
    "DB_NAME": os.environ.get("TEST_DB_NAME", "assistant_test"),
}


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The test settings in the environment; a test changes or drops a variable with ``monkeypatch``."""
    for name in TEST_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    for name, value in TEST_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    return dict(TEST_ENVIRONMENT)


BASE_DIR = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = URL.create(
    "postgresql+asyncpg",
    username=TEST_ENVIRONMENT["DB_USER"],
    password=TEST_ENVIRONMENT["DB_PASSWORD"],
    host=TEST_ENVIRONMENT["DB_HOST"],
    port=int(TEST_ENVIRONMENT["DB_PORT"]),
    database=TEST_ENVIRONMENT["DB_NAME"],
)
if not TEST_DATABASE_URL.database.endswith("_test"):
    raise RuntimeError("The test suite rebuilds its database and refuses to run on one not named *_test")


def alembic_config() -> Config:
    config = Config(str(BASE_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BASE_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL.render_as_string(hide_password=False))
    return config


def rebuild_schema() -> None:
    async def reset() -> None:
        engine = create_async_engine(TEST_DATABASE_URL)
        async with engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))
        await engine.dispose()

    asyncio.run(reset())
    command.upgrade(alembic_config(), "head")


@pytest.fixture(scope="session")
def migrated_schema() -> Iterator[None]:
    rebuild_schema()
    yield


@pytest_asyncio.fixture
async def database(migrated_schema) -> AsyncIterator[Database]:
    database = Database(TEST_DATABASE_URL, TokenCipher(TEST_ENVIRONMENT["ENCRYPTION_KEY"]))
    async with database.engine.begin() as connection:
        tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
        await connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield database
    await database.dispose()
