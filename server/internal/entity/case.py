import datetime
import enum
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from internal.crypto import EncryptedJson
from internal.entity.base import Base, now_column


class CaseState(enum.StrEnum):
    INVESTIGATING = "investigating"
    WAITING_CLIENT = "waiting_client"
    WAITING_FIX = "waiting_fix"
    WAITING_OWNER = "waiting_owner"
    RESOLVED = "resolved"


class Incident(Base):
    """One failure several cases come from: the responsible person is called about it once."""

    __tablename__ = "incident"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: What the cases have in common, in the words of the model: the cause, not a symptom.
    cause: Mapped[str] = mapped_column(String(255), unique=True)
    description: Mapped[str] = mapped_column(Text)
    escalated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime.datetime] = now_column()

    cases: Mapped[list["Case"]] = relationship(back_populates="incident")


class Case(Base):
    """A case is a thread: the message it was asked in and everything answered under it."""

    __tablename__ = "case"
    __table_args__ = (UniqueConstraint("chat_id", "root_message_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int]
    #: The message the assistant was asked in: its answers are replies to it.
    root_message_id: Mapped[int]
    #: Whether the case came in a private chat: then the responsible person is called in a private message.
    private: Mapped[bool]
    #: ``sub`` of the person who asked.
    staff_subject: Mapped[str] = mapped_column(String(255))
    #: The account of the client the case is about, once it is known for certain.
    client_account: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[CaseState] = mapped_column(
        Enum(CaseState, native_enum=False, length=32, values_callable=lambda states: [s.value for s in states])
    )
    #: What the case waits for, in the words of the model: checked again on a schedule while waiting for a fix.
    waiting_for: Mapped[str | None] = mapped_column(Text)
    #: The responsible person answered in the thread themselves: the assistant writes no more there.
    owner_took_over: Mapped[bool] = mapped_column(default=False)
    #: The conversation with the model, only ever appended to. Encrypted: it holds the client's files.
    conversation: Mapped[list[dict[str, Any]]] = mapped_column(EncryptedJson, default=list)
    #: Label to value of the personal data of the case: the model sees labels only. Encrypted.
    labels: Mapped[dict[str, str]] = mapped_column(EncryptedJson, default=dict)
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("incident.id"))
    created_at: Mapped[datetime.datetime] = now_column()
    updated_at: Mapped[datetime.datetime] = now_column()

    incident: Mapped[Incident | None] = relationship(back_populates="cases")


class MessageRole(enum.StrEnum):
    #: The message the case was asked in, or one the staff wrote in its thread.
    REQUEST = "request"
    #: An answer of the assistant, rated with the buttons under it.
    ANSWER = "answer"
    #: The question what was wrong with an answer: a reply to it starts the case over.
    FEEDBACK_QUESTION = "feedback_question"
    #: Anything else the assistant wrote in the thread.
    NOTE = "note"


class CaseMessage(Base):
    """A message of a case thread: a reply to any of them continues the case."""

    __tablename__ = "case_message"

    chat_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    message_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    case_id: Mapped[int] = mapped_column(ForeignKey("case.id", ondelete="CASCADE"))
    role: Mapped[MessageRole] = mapped_column(
        Enum(MessageRole, native_enum=False, length=32, values_callable=lambda roles: [r.value for r in roles])
    )


class TurnKind(enum.StrEnum):
    #: The staff asked about the case, or wrote in its thread.
    REQUEST = "request"
    #: The staff called a responsible person: it is a case only if the message is about one.
    OWNER_MENTION = "owner_mention"
    #: The staff said what was wrong with an answer: the case is looked at again.
    RETRY = "retry"
    #: A responsible person answered in the thread: the assistant offers them a rule and writes no more there.
    OWNER_ANSWER = "owner_answer"
    #: The schedule checks whether the fix the case waits for reached the clients.
    FOLLOW_UP = "follow_up"


class CaseTurn(Base):
    """A turn of a case waiting for the model: kept, so that a restart loses no request."""

    __tablename__ = "case_turn"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("case.id", ondelete="CASCADE"), index=True)
    kind: Mapped[TurnKind] = mapped_column(
        Enum(TurnKind, native_enum=False, length=32, values_callable=lambda kinds: [k.value for k in kinds])
    )
    #: The message, as the listener read it: who wrote, the text, the files, what it quotes. Encrypted.
    payload: Mapped[dict[str, Any]] = mapped_column(EncryptedJson)
    created_at: Mapped[datetime.datetime] = now_column()
    done_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
