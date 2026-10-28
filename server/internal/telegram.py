"""The Telegram Bot API as the assistant uses it. Nothing else in the service knows the API."""

import json
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from loguru import logger

API_URL = "https://api.telegram.org"
#: How long a call that is not a long poll may take.
CALL_TIMEOUT_SECONDS = 30.0
#: The longest text of one message (core.telegram.org/bots/api, sendMessage: 1-4096 characters after
#: entities parsing). Counted here in UTF-16 code units, which is never fewer than characters.
MAX_MESSAGE_LENGTH = 4096
#: The Bot API hands out files up to this size.
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_UPDATES = ["message", "my_chat_member", "callback_query"]


class TelegramUnavailable(Exception):
    """The call did not get through or Telegram could not serve it now. It may be repeated later."""


class ChatClosed(Exception):
    """Telegram will not deliver to this chat: the person blocked the bot or the chat is gone."""


@dataclass(frozen=True)
class Attachment:
    kind: Literal["photo", "video", "animation", "document", "voice", "video_note", "audio", "sticker"]
    file_id: str
    #: The name the sender gave the file, when there is one.
    name: str | None
    mime_type: str | None
    size: int | None


@dataclass(frozen=True)
class Quoted:
    """The message another message answers, or carries forwarded: the client's message, usually."""

    message_id: int
    sender_id: int | None
    text: str
    attachments: tuple[Attachment, ...]


@dataclass(frozen=True)
class Said:
    """Someone wrote in a chat the bot is in."""

    update_id: int
    chat_id: int
    chat_type: Literal["private", "group", "supergroup", "channel"]
    message_id: int
    #: The topic of a forum group, so that an answer lands in the same topic.
    thread_id: int | None
    #: The number of the person who sent it; None when the message has no person behind it.
    sender_id: int | None
    sender_username: str | None
    sender_is_bot: bool
    #: Sent on behalf of a chat: an anonymous administrator, a channel, a group.
    on_behalf_of_chat: bool
    text: str
    #: User names mentioned with an at sign, without it.
    mentioned_usernames: frozenset[str]
    #: Numbers of people mentioned by name, for people without a user name.
    mentioned_ids: frozenset[int]
    attachments: tuple[Attachment, ...]
    #: The message this one replies to.
    reply_to: Quoted | None
    #: Whether this message carries a forwarded one: the forwarded text and files are this message's own.
    forwarded: bool


@dataclass(frozen=True)
class Pressed:
    """Someone pressed a button under a message of the bot."""

    update_id: int
    chat_id: int
    message_id: int
    sender_id: int
    #: What Telegram wants answered, so that the button stops spinning.
    press_id: str
    data: str


@dataclass(frozen=True)
class MembershipChanged:
    """The bot was added to a chat, removed from it, or blocked."""

    update_id: int
    chat_id: int
    chat_type: str
    status: str


Event = Said | Pressed | MembershipChanged


@dataclass(frozen=True)
class Button:
    text: str
    #: What the bot gets back when the button is pressed: 1 to 64 bytes.
    data: str


#: Rows of buttons under a message.
Keyboard = list[list[Button]]


@dataclass(frozen=True)
class Entity:
    """A piece of formatting, in UTF-16 code units as the Bot API counts them."""

    type: str
    offset: int
    length: int
    #: The person a ``text_mention`` points to: it works for people without a user name.
    user_id: int | None = None

    def to_api(self) -> dict[str, Any]:
        entity: dict[str, Any] = {"type": self.type, "offset": self.offset, "length": self.length}
        if self.user_id is not None:
            entity["user"] = {"id": self.user_id}
        return entity


@dataclass(frozen=True)
class Text:
    """Text with its formatting: what the bot sends."""

    text: str
    entities: tuple[Entity, ...] = field(default=())


@dataclass(frozen=True)
class RichMessage:
    """A formatted message of blocks (``sendRichMessage``): headings, lists, tables, quotes."""

    blocks: tuple[dict[str, Any], ...]

    def plain(self) -> str:
        """The text of the blocks without their formatting."""
        return "\n".join(_plain(block) for block in self.blocks)


def _plain(node: Any) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_plain(item) for item in node)
    if node.get("type") == "table":
        return "\n".join(" | ".join(_plain(cell.get("text", "")) for cell in row) for row in node["cells"])
    if node.get("type") == "list":
        return "\n".join("- " + "\n".join(_plain(block) for block in item["blocks"]) for item in node["items"])
    if "blocks" in node:
        return "\n".join(_plain(block) for block in node["blocks"])
    return _plain(node.get("text", ""))


@dataclass(frozen=True)
class Update:
    id: int
    #: What the update says, when the assistant has a use for it.
    event: Event | None


class TelegramGateway:
    """Gateway to the Bot API of one bot."""

    def __init__(self, token: str, proxy_url: str | None) -> None:
        self._base_url = f"{API_URL}/bot{token}"
        self._file_url = f"{API_URL}/file/bot{token}"
        self._client = httpx.AsyncClient(proxy=proxy_url, timeout=CALL_TIMEOUT_SECONDS)

    async def close(self) -> None:
        await self._client.aclose()

    async def get_updates(self, offset: int | None, wait_seconds: int) -> list[Update]:
        """Updates from ``offset`` on, waiting up to ``wait_seconds`` for the first one."""
        payload: dict[str, Any] = {"timeout": wait_seconds, "allowed_updates": ALLOWED_UPDATES}
        if offset is not None:
            payload["offset"] = offset
        result = await self._call("getUpdates", json=payload, timeout=wait_seconds + CALL_TIMEOUT_SECONDS)
        return [Update(id=raw["update_id"], event=parse_event(raw)) for raw in result]

    async def send_message(
        self,
        chat_id: int,
        text: Text,
        *,
        reply_to: int | None = None,
        thread_id: int | None = None,
        keyboard: Keyboard | None = None,
    ) -> list[int]:
        """Send a message; a text longer than one message goes as several, buttons under the last.

        Returns the numbers of the messages in the chat.
        """
        parts = split_text(text, MAX_MESSAGE_LENGTH)
        numbers = []
        for index, part in enumerate(parts):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": part.text,
                "entities": [entity.to_api() for entity in part.entities],
                "link_preview_options": {"is_disabled": True},
            }
            self._place(payload, reply_to, thread_id, keyboard if index == len(parts) - 1 else None)
            sent = await self._call("sendMessage", json=payload)
            numbers.append(sent["message_id"])
        return numbers

    async def send_rich(
        self,
        chat_id: int,
        message: RichMessage,
        *,
        reply_to: int | None = None,
        thread_id: int | None = None,
        keyboard: Keyboard | None = None,
    ) -> int:
        """Send a formatted message; returns its number in the chat."""
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            # Emails and phones are hidden by the assistant; the API must not turn them into links.
            "rich_message": {"blocks": list(message.blocks), "skip_entity_detection": True},
        }
        self._place(payload, reply_to, thread_id, keyboard)
        sent = await self._call("sendRichMessage", json=payload)
        return sent["message_id"]

    async def send_media(
        self,
        chat_id: int,
        kind: Literal["photo", "video"],
        content: bytes | str,
        file_name: str,
        *,
        caption: Text | None = None,
        reply_to: int | None = None,
        thread_id: int | None = None,
    ) -> int:
        """Send a picture or a video under a spoiler: it opens on a tap.

        ``content`` is the file itself, or the id of a file Telegram already has.
        """
        data: dict[str, Any] = {"chat_id": chat_id, "has_spoiler": True}
        if caption is not None:
            data["caption"] = caption.text
            data["caption_entities"] = [entity.to_api() for entity in caption.entities]
        self._place(data, reply_to, thread_id, None)
        method = "sendPhoto" if kind == "photo" else "sendVideo"
        if isinstance(content, str):
            sent = await self._call(method, json={**data, kind: content})
            return sent["message_id"]
        # Multipart fields are strings: the nested ones go as JSON.
        form = {key: value if isinstance(value, str) else json.dumps(value) for key, value in data.items()}
        sent = await self._call(method, data=form, files={kind: (file_name, content)})
        return sent["message_id"]

    async def download(self, file_id: str) -> bytes:
        """The content of a file someone sent. Raises ``TelegramUnavailable`` when it cannot be had."""
        described = await self._call("getFile", json={"file_id": file_id})
        if "file_path" not in described or described.get("file_size", 0) > MAX_DOWNLOAD_BYTES:
            raise TelegramUnavailable("The file is too large for the Bot API")
        try:
            response = await self._client.get(f"{self._file_url}/{described['file_path']}")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # The address carries the token of the bot: the log gets the kind of failure alone.
            logger.warning("Telegram file download failed: {}", type(exc).__name__)
            raise TelegramUnavailable("Telegram file download failed") from exc
        return response.content

    async def leave_chat(self, chat_id: int) -> None:
        await self._call("leaveChat", json={"chat_id": chat_id})

    async def answer_press(self, press_id: str, text: str) -> None:
        await self._call("answerCallbackQuery", json={"callback_query_id": press_id, "text": text})

    async def set_keyboard(self, chat_id: int, message_id: int, keyboard: Keyboard | None) -> None:
        await self._call(
            "editMessageReplyMarkup",
            json={"chat_id": chat_id, "message_id": message_id, "reply_markup": _markup(keyboard or [])},
        )

    @staticmethod
    def _place(payload: dict[str, Any], reply_to: int | None, thread_id: int | None, keyboard: Keyboard | None) -> None:
        if reply_to is not None:
            # The message it answers may be gone: the answer is sent all the same.
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        if thread_id is not None:
            payload["message_thread_id"] = thread_id
        if keyboard:
            payload["reply_markup"] = _markup(keyboard)

    async def _call(self, method: str, *, timeout: float = CALL_TIMEOUT_SECONDS, **request: Any) -> Any:
        try:
            response = await self._client.post(f"{self._base_url}/{method}", timeout=timeout, **request)
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # The address carries the token of the bot: the log gets the method alone.
            logger.warning("Telegram call {} failed: {}", method, type(exc).__name__)
            raise TelegramUnavailable(f"Telegram call {method} failed") from exc

        if body.get("ok") is True:
            return body["result"]

        description = str(body.get("description", ""))
        if response.status_code == 403 or (response.status_code == 400 and "chat not found" in description):
            raise ChatClosed(description)
        logger.warning("Telegram refused {}: {} {}", method, response.status_code, description)
        raise TelegramUnavailable(f"Telegram refused {method}: {response.status_code}")


def _markup(keyboard: Keyboard) -> dict[str, Any]:
    return {
        "inline_keyboard": [[{"text": button.text, "callback_data": button.data} for button in row] for row in keyboard]
    }


def utf16_length(text: str) -> int:
    """The length of a text as the Bot API counts offsets: in UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def split_text(text: Text, limit: int) -> list[Text]:
    """Cut a text into pieces of at most ``limit`` UTF-16 units, at a line break where there is one.

    Nothing is dropped: a piece of formatting that crosses a cut is carried on in the next piece.
    """
    units = text.text.encode("utf-16-le")
    total = len(units) // 2
    if total <= limit:
        return [text]

    def unit(index: int) -> int:
        return int.from_bytes(units[index * 2 : index * 2 + 2], "little")

    cuts = []
    start = 0
    while total - start > limit:
        end = start + limit
        # A low surrogate is the second half of a character: never cut before it.
        while 0xDC00 <= unit(end) <= 0xDFFF:
            end -= 1
        newline = max((i for i in range(start + 1, end) if unit(i - 1) == 0x0A), default=None)
        if newline is not None and newline - start > limit // 2:
            end = newline
        cuts.append((start, end))
        start = end
    cuts.append((start, total))

    pieces = []
    for start, end in cuts:
        entities = []
        for entity in text.entities:
            left, right = max(entity.offset, start), min(entity.offset + entity.length, end)
            if left < right:
                entities.append(Entity(entity.type, left - start, right - left, entity.user_id))
        pieces.append(Text(units[start * 2 : end * 2].decode("utf-16-le"), tuple(entities)))
    return pieces


def _from_utf16(text: str, offset: int, length: int) -> str:
    encoded = text.encode("utf-16-le")
    return encoded[offset * 2 : (offset + length) * 2].decode("utf-16-le")


ATTACHMENT_KINDS = ("video", "animation", "document", "voice", "video_note", "audio", "sticker")


def _attachments(message: dict[str, Any]) -> tuple[Attachment, ...]:
    found = []
    if message.get("photo"):
        # Telegram lists the sizes of one picture from the smallest; the largest is read.
        largest = message["photo"][-1]
        found.append(Attachment("photo", largest["file_id"], None, "image/jpeg", largest.get("file_size")))
    for kind in ATTACHMENT_KINDS:
        if kind in message:
            item = message[kind]
            found.append(
                Attachment(kind, item["file_id"], item.get("file_name"), item.get("mime_type"), item.get("file_size"))
            )
    return tuple(found)


def _text(message: dict[str, Any]) -> str:
    return str(message.get("text") or message.get("caption") or "")


def _mentions(message: dict[str, Any]) -> tuple[frozenset[str], frozenset[int]]:
    text = _text(message)
    usernames: set[str] = set()
    ids: set[int] = set()
    for entity in message.get("entities") or message.get("caption_entities") or []:
        if entity["type"] == "mention":
            usernames.add(_from_utf16(text, entity["offset"], entity["length"]).removeprefix("@").lower())
        elif entity["type"] == "text_mention" and "user" in entity:
            ids.add(entity["user"]["id"])
    return frozenset(usernames), frozenset(ids)


def parse_event(raw: dict[str, Any]) -> Event | None:
    update_id = raw["update_id"]
    message = raw.get("message")
    if message is not None:
        sender = message.get("from") or {}
        usernames, ids = _mentions(message)
        replied = message.get("reply_to_message")
        reply_to = None
        if replied is not None:
            reply_to = Quoted(
                message_id=replied["message_id"],
                sender_id=(replied.get("from") or {}).get("id"),
                text=_text(replied),
                attachments=_attachments(replied),
            )
        return Said(
            update_id=update_id,
            chat_id=message["chat"]["id"],
            chat_type=message["chat"]["type"],
            message_id=message["message_id"],
            thread_id=message.get("message_thread_id") if message.get("is_topic_message") else None,
            sender_id=sender.get("id"),
            sender_username=sender.get("username"),
            sender_is_bot=bool(sender.get("is_bot")),
            on_behalf_of_chat="sender_chat" in message,
            text=_text(message),
            mentioned_usernames=usernames,
            mentioned_ids=ids,
            attachments=_attachments(message),
            reply_to=reply_to,
            forwarded="forward_origin" in message,
        )

    press = raw.get("callback_query")
    if press is not None:
        under = press.get("message")
        if under is None or "data" not in press:
            return None
        return Pressed(
            update_id=update_id,
            chat_id=under["chat"]["id"],
            message_id=under["message_id"],
            sender_id=press["from"]["id"],
            press_id=press["id"],
            data=press["data"],
        )

    membership = raw.get("my_chat_member")
    if membership is not None:
        return MembershipChanged(
            update_id=update_id,
            chat_id=membership["chat"]["id"],
            chat_type=membership["chat"]["type"],
            status=membership["new_chat_member"]["status"],
        )
    return None
