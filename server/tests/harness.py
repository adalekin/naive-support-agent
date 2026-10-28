"""The whole assistant over fakes: Telegram over pytest-httpx, the provider, the model, a source."""

import json
import re
from itertools import count
from typing import Any

import httpx

from internal.access.guard import AccessGuard
from internal.access.login import DeviceLoginProcess
from internal.access.oidc import OidcProvider
from internal.agent.classifier import CaseClassifier
from internal.agent.prompt import system_prompt
from internal.agent.runner import CaseAgent
from internal.bot.handlers import HandleMembership, HandleMessage, HandlePress, Policy, UpdateDispatcher
from internal.bot.listener import TelegramListener
from internal.bot.postman import Postman
from internal.bot.worker import CaseWorker, FollowUpScheduler
from internal.config.instructions import InstructionSet
from internal.crypto import TokenCipher
from internal.repository import StaffRepository
from internal.sources.attachments import AttachmentReader
from internal.sources.base import Source, SourceRegistry, ToolOutput, ToolSpec
from internal.telegram import MembershipChanged, Pressed, Said, TelegramGateway
from internal.words import Words
from tests.conftest import TEST_ENVIRONMENT
from tests.fake_model import FakeModel
from tests.fakes import CLAIMS, ISSUER, ROLES, FakeProvider

BOT = "https://api.telegram.org/bot1234:test-token"
GROUP = -1001
cipher = TokenCipher(TEST_ENVIRONMENT["ENCRYPTION_KEY"])
INSTRUCTIONS = InstructionSet(text="Refunds are made by billing.", sources=(), personal_fields=frozenset({"name"}))


class Database(Source):
    name = "db"
    description = "The product database."

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def list_tools(self):
        return [ToolSpec("query", "Run a read-only query.", {"type": "object", "properties": {}})]

    async def call(self, tool, arguments):
        return ToolOutput(json.dumps(self.rows))


class FakeTelegram:
    """The Bot API: updates handed out once, every sent message kept."""

    def __init__(self, httpx_mock) -> None:
        self.updates: list[dict[str, Any]] = []
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self._numbers = count(1000)
        self._update_ids = count(1)
        httpx_mock.add_callback(self._handle, url=re.compile(rf"{BOT}/.*"), is_reusable=True, is_optional=True)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content and method != "sendPhoto" else {}
        if method == "getUpdates":
            offset = body.get("offset") or 0
            ready = [update for update in self.updates if update["update_id"] >= offset]
            return httpx.Response(200, json={"ok": True, "result": ready})
        self.sent.append((method, body))
        if method in ("sendMessage", "sendPhoto", "sendVideo"):
            number = next(self._numbers)
            body["sent_as"] = number
            return httpx.Response(200, json={"ok": True, "result": {"message_id": number}})
        return httpx.Response(200, json={"ok": True, "result": True})

    def message(self, chat_id: int, sender: int | None, text: str, *, chat_type: str = "supergroup", **extra) -> int:
        number = next(self._numbers)
        message = {"message_id": number, "chat": {"id": chat_id, "type": chat_type}, "date": 1, "text": text, **extra}
        if sender is not None:
            message["from"] = {"id": sender, "is_bot": False, "first_name": "P", **extra.pop("sender_extra", {})}
        entities = []
        for word in text.split():
            if word.startswith("@"):
                entities.append({"type": "mention", "offset": text.index(word), "length": len(word)})
        if entities:
            message["entities"] = entities
        self.updates.append({"update_id": next(self._update_ids), "message": message})
        return number

    def private(self, sender: int, text: str, **extra) -> int:
        return self.message(sender, sender, text, chat_type="private", **extra)

    def press(self, sender: int, chat_id: int, message_id: int, data: str) -> None:
        self.updates.append(
            {
                "update_id": next(self._update_ids),
                "callback_query": {
                    "id": f"q{message_id}",
                    "from": {"id": sender, "is_bot": False, "first_name": "P"},
                    "message": {"message_id": message_id, "chat": {"id": chat_id, "type": "supergroup"}, "date": 1},
                    "data": data,
                },
            }
        )

    def added_to(self, chat_id: int, chat_type: str = "group") -> None:
        bot = {"id": 2, "is_bot": True, "first_name": "B"}
        self.updates.append(
            {
                "update_id": next(self._update_ids),
                "my_chat_member": {
                    "chat": {"id": chat_id, "type": chat_type},
                    "from": {"id": 1, "is_bot": False, "first_name": "X"},
                    "date": 1,
                    "old_chat_member": {"status": "left", "user": bot},
                    "new_chat_member": {"status": "member", "user": bot},
                },
            }
        )

    def texts(self, chat_id: int | None = None) -> list[str]:
        return [
            body["text"]
            for method, body in self.sent
            if method == "sendMessage" and (chat_id is None or body["chat_id"] == chat_id)
        ]

    def number_of(self, text_start: str) -> int:
        """The number the bot's last message starting with these words got."""
        return [body["sent_as"] for body in self.sent_messages() if body["text"].startswith(text_start)][-1]

    def in_thread(self, message_id: int, chat_id: int = GROUP) -> dict[str, Any]:
        return {"message_id": message_id, "chat": {"id": chat_id, "type": "supergroup"}, "date": 1}

    def sent_messages(self) -> list[dict[str, Any]]:
        return [body for method, body in self.sent if method == "sendMessage"]


class Harness:
    def __init__(self, database, httpx_mock, model: FakeModel, groups=frozenset({GROUP})) -> None:
        self.database = database
        self.telegram = FakeTelegram(httpx_mock)
        self.provider = FakeProvider(httpx_mock)
        self.provider.account("sub-anna", "anna", ["support"])
        self.provider.account("sub-oleg", "oleg", ["support", "owner", "escalation"])
        self.provider.account("sub-eve", "eve", [])
        self.model = model
        self.source = Database()
        gateway = TelegramGateway("1234:test-token", None)
        oidc = OidcProvider(ISSUER, "support-assistant", "client-secret", CLAIMS)
        words = Words("en")
        self.guard = AccessGuard(database, oidc, cipher, *ROLES)
        self.login = DeviceLoginProcess(oidc, self.guard, cipher, words, groups, ROLES[0])
        self.registry = SourceRegistry([self.source], {"db": ["query"]})
        self.postman = Postman(gateway, database)
        client = model.client()
        self.worker = CaseWorker(
            database=database,
            agent=CaseAgent(client, "claude-opus-5-5", "high", 1_000_000, system_prompt(INSTRUCTIONS)),
            classifier=CaseClassifier(client, "claude-opus-5-5"),
            registry=self.registry,
            reader=AttachmentReader(gateway),
            guard=self.guard,
            words=words,
            personal_fields=INSTRUCTIONS.personal_fields,
            postman=self.postman,
            concurrency=4,
        )
        policy = Policy("assistant_test_bot", groups)
        self.listener = TelegramListener(
            gateway,
            database,
            UpdateDispatcher(
                {
                    Said: HandleMessage(policy, self.guard, self.login, words),
                    Pressed: HandlePress(self.guard, words),
                    MembershipChanged: HandleMembership(policy),
                }
            ),
            self.postman,
        )
        self.follow_up = FollowUpScheduler(database, self.worker, 3600)

    async def start(self) -> "Harness":
        await self.registry.start()
        return self

    async def bind(self, telegram_user_id: int, sub: str, username: str | None = None) -> None:
        async with self.database.unit_of_work() as session:
            name = self.provider.accounts[sub]["name"]
            await StaffRepository(session).bind(
                telegram_user_id, sub, name, cipher.seal(self.provider.issue(sub)), username
            )

    async def step(self) -> None:
        """Hand out the updates, then run every turn they queued."""
        await self.listener.read_once(wait_seconds=0)
        await self.worker.drain_all()
