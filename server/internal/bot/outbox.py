"""What the bot says, collected while an event is handled and sent after its transaction commits.

Nobody is told of something that did not happen: a rolled back event sends nothing.
"""

from dataclasses import dataclass, field
from typing import Literal

from internal.entity import MessageRole
from internal.telegram import Keyboard, Text


@dataclass(frozen=True)
class Send:
    chat_id: int
    text: Text
    reply_to: int | None = None
    thread_id: int | None = None
    keyboard: Keyboard | None = None
    #: The case the message belongs to: a reply to it continues the case.
    case_id: int | None = None
    role: MessageRole = MessageRole.NOTE


@dataclass(frozen=True)
class SendMedia:
    chat_id: int
    kind: Literal["photo", "video"]
    #: The file itself, or the id of a file Telegram already has.
    content: bytes | str
    file_name: str
    caption: Text | None = None
    reply_to: int | None = None
    thread_id: int | None = None
    case_id: int | None = None
    role: MessageRole = MessageRole.NOTE


@dataclass(frozen=True)
class AnswerPress:
    press_id: str
    text: str


@dataclass(frozen=True)
class ChangeKeyboard:
    chat_id: int
    message_id: int
    keyboard: Keyboard | None


@dataclass(frozen=True)
class Leave:
    chat_id: int


Reply = Send | SendMedia | AnswerPress | ChangeKeyboard | Leave


@dataclass
class Outbox:
    replies: list[Reply] = field(default_factory=list)

    def add(self, reply: Reply) -> None:
        self.replies.append(reply)

    def say(self, chat_id: int, text: str, **placement: int | None) -> None:
        self.replies.append(Send(chat_id, Text(text), **placement))
