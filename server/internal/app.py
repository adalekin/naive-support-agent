"""The composition root: builds the assistant from its settings and runs its loops."""

import asyncio
import datetime
import tempfile
from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient
from loguru import logger
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from internal.access.guard import AccessGuard
from internal.access.login import DeviceLoginProcess
from internal.access.oidc import OidcProvider
from internal.agent.classifier import CaseClassifier
from internal.agent.prompt import system_prompt
from internal.agent.runner import CaseAgent
from internal.bot.handlers import HandleMembership, HandleMessage, HandlePress, Policy, UpdateDispatcher
from internal.bot.listener import TelegramListener
from internal.bot.outbox import Outbox
from internal.bot.postman import Postman
from internal.bot.retention import RetentionSweeper
from internal.bot.worker import CaseWorker, FollowUpScheduler
from internal.config import InstructionSet, Settings
from internal.config.instructions import McpSourceSpec, RepositorySourceSpec
from internal.crypto import TokenCipher
from internal.db import Database
from internal.sources.attachments import AttachmentReader
from internal.sources.base import Source, SourceRegistry, SourceUnavailable
from internal.sources.mcp import McpSource
from internal.sources.repository import RepositorySource
from internal.telegram import MembershipChanged, Pressed, Said, TelegramGateway, TelegramUnavailable
from internal.words import Words

#: How many cases are investigated at once.
CONCURRENT_CASES = 4
LOGIN_POLL_SECONDS = 2
RETRY_SECONDS = 5
#: How often a source that is down is started again.
SOURCE_RETRY_SECONDS = 60
BASE_DIR = Path(__file__).resolve().parents[1]


class Assistant:
    def __init__(self, settings: Settings, instructions: InstructionSet, clones: Path | None = None) -> None:
        self.settings = settings
        self.instructions = instructions
        cipher = TokenCipher(settings.ENCRYPTION_KEY.get_secret_value())
        self.database = Database(settings.database_url, cipher)
        self.telegram = TelegramGateway(settings.TELEGRAM_BOT_TOKEN.get_secret_value(), settings.telegram_proxy_url)
        self.oidc = OidcProvider(
            settings.OIDC_ISSUER,
            settings.OIDC_CLIENT_ID,
            settings.OIDC_CLIENT_SECRET.get_secret_value(),
            [settings.OIDC_ROLES_CLAIM, settings.ANALYTICS_ROLES_CLAIM],
        )
        words = Words(settings.LOCALE)
        self.guard = AccessGuard(
            self.database, self.oidc, cipher, settings.role_support, settings.role_owner, settings.role_analytics
        )
        self.login = DeviceLoginProcess(
            self.oidc, self.guard, cipher, words, settings.TELEGRAM_GROUPS, settings.role_support
        )

        self.repositories: list[RepositorySource] = []
        sources: list[Source] = []
        allowed: dict[str, list[str]] = {}
        clones = clones or Path(tempfile.gettempdir()) / "assistant-clones"
        for spec in instructions.sources:
            if isinstance(spec, McpSourceSpec):
                sources.append(McpSource(spec))
                allowed[spec.name] = spec.tools
            elif isinstance(spec, RepositorySourceSpec):
                repository = RepositorySource(spec, clones)
                self.repositories.append(repository)
                sources.append(repository)
        self.registry = SourceRegistry(sources, allowed)

        self.model_proxy = settings.model_proxy_url
        self.model = AsyncAnthropic(
            api_key=settings.ANTHROPIC_API_KEY.get_secret_value(),
            http_client=DefaultAsyncHttpxClient(proxy=self.model_proxy) if self.model_proxy is not None else None,
        )
        self.postman = Postman(self.telegram, self.database)
        self.worker = CaseWorker(
            database=self.database,
            agent=CaseAgent(
                self.model,
                settings.MODEL,
                settings.MODEL_EFFORT,
                settings.CASE_TOKEN_BUDGET,
                system_prompt(instructions),
            ),
            classifier=CaseClassifier(self.model, settings.MODEL),
            registry=self.registry,
            reader=AttachmentReader(self.telegram),
            guard=self.guard,
            words=words,
            personal_fields=instructions.personal_fields,
            postman=self.postman,
            concurrency=CONCURRENT_CASES,
        )
        policy = Policy(settings.TELEGRAM_BOT_USERNAME, settings.TELEGRAM_GROUPS)
        dispatcher = UpdateDispatcher(
            {
                Said: HandleMessage(policy, self.guard, self.login, words),
                Pressed: HandlePress(self.guard, words),
                MembershipChanged: HandleMembership(policy),
            }
        )
        self.listener = TelegramListener(self.telegram, self.database, dispatcher, self.postman)
        self.retention = RetentionSweeper(self.database, settings.CASE_RETENTION_DAYS)
        self.follow_up = FollowUpScheduler(self.database, self.worker, settings.FOLLOWUP_INTERVAL_MINUTES * 60)

    async def wait_for_schema(self) -> None:
        """Wait until the migrations Job brought the database to this image's revision."""
        config = AlembicConfig(str(BASE_DIR / "alembic.ini"))
        config.set_main_option("script_location", str(BASE_DIR / "migrations"))
        head = ScriptDirectory.from_config(config).get_current_head()
        while True:
            try:
                async with self.database.unit_of_work() as session:
                    current = await session.scalar(text("SELECT version_num FROM alembic_version"))
            except ProgrammingError:
                # No migration ran yet: there is no table of versions.
                current = None
            if current == head:
                return
            logger.info("The database is at revision {}, waiting for {}", current, head)
            await asyncio.sleep(RETRY_SECONDS)

    async def run(self) -> None:
        await self.wait_for_schema()
        await self.registry.start()
        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self._listen(), name="telegram")
                tasks.create_task(self.worker.run_forever(), name="worker")
                tasks.create_task(self._poll_logins(), name="logins")
                tasks.create_task(self.follow_up.run_forever(), name="follow-up")
                tasks.create_task(self._restart_sources(), name="sources")
                tasks.create_task(self.retention.run_forever(), name="retention")
                for repository in self.repositories:
                    tasks.create_task(self._sync(repository), name=f"sync-{repository.name}")
        finally:
            await self.registry.close()
            await self.telegram.close()
            await self.oidc.close()
            await self.database.dispose()

    async def _listen(self) -> None:
        while True:
            try:
                if await self.listener.read_once():
                    self.worker.wake.set()
            except TelegramUnavailable:
                await asyncio.sleep(RETRY_SECONDS)

    async def _poll_logins(self) -> None:
        while True:
            outbox = Outbox()
            async with self.database.unit_of_work() as session:
                await self.login.poll_due(session, outbox, datetime.datetime.now(datetime.UTC))
            await self.postman.deliver(outbox)
            await asyncio.sleep(LOGIN_POLL_SECONDS)

    async def _restart_sources(self) -> None:
        while True:
            await asyncio.sleep(SOURCE_RETRY_SECONDS)
            await self.registry.restart_down()

    @staticmethod
    async def _sync(repository: RepositorySource) -> None:
        while True:
            await asyncio.sleep(repository.sync_seconds)
            try:
                await repository.sync()
            except SourceUnavailable as unavailable:
                logger.warning("Repository {} was not brought up to date: {}", repository.name, unavailable)
