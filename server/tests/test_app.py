import asyncio

import pytest
from anthropic import AsyncAnthropicGoogleCloud
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


async def test_model_is_reached_through_the_proxy_of_the_installation(environment, monkeypatch):
    monkeypatch.setenv("MODEL_EGRESS", "proxy")
    monkeypatch.setenv("MODEL_PROXY_URL", "socks5://user:secret@proxy.test:1080")
    assistant = Assistant(load_settings(), INSTRUCTIONS)
    try:
        assert assistant.model_proxy == "socks5://user:secret@proxy.test:1080"
        assert assistant.model._client._mounts, "the HTTP client of the model has no proxy"
    finally:
        await assistant.database.dispose()


async def test_google_cloud_model_uses_adc_instead_of_the_direct_anthropic_key(environment, monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "anthropic_google_cloud")
    monkeypatch.setenv("ANTHROPIC_GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("ANTHROPIC_GOOGLE_CLOUD_WORKSPACE_ID", "test-workspace")
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assistant = Assistant(load_settings(), INSTRUCTIONS)
    try:
        assert isinstance(assistant.model, AsyncAnthropicGoogleCloud)
        assert str(assistant.model.base_url) == (
            "https://claude.googleapis.com/v1/projects/test-project/locations/global/workspaces/test-workspace/invoke/"
        )
    finally:
        await assistant.model.close()
        await assistant.database.dispose()


async def test_model_is_reached_directly_without_a_proxy(environment):
    assistant = Assistant(load_settings(), INSTRUCTIONS)
    try:
        assert assistant.model_proxy is None
        assert not assistant.model._client._mounts
    finally:
        await assistant.database.dispose()
