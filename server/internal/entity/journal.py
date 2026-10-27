import datetime
import enum
from typing import Any

from sqlalchemy import Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from internal.entity.base import Base, now_column


class JournalKind(enum.StrEnum):
    #: A person with access asked about a case.
    REQUEST = "request"
    #: Someone without access addressed the assistant. Numbers only, never the text.
    REFUSED = "refused"
    BOUND = "bound"
    UNBOUND = "unbound"
    #: The assistant called a responsible person.
    ESCALATED = "escalated"
    #: A call to a source: which tool, for which case.
    SOURCE_CALL = "source_call"
    #: What a request to the model cost.
    USAGE = "usage"
    #: A person rated an answer.
    FEEDBACK = "feedback"


class JournalEntry(Base):
    """The journal is only ever appended to."""

    __tablename__ = "journal_entry"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime.datetime] = now_column()
    kind: Mapped[JournalKind] = mapped_column(
        Enum(JournalKind, native_enum=False, length=32, values_callable=lambda kinds: [k.value for k in kinds])
    )
    #: The account at the provider of the person concerned, when there is one.
    subject: Mapped[str | None] = mapped_column(String(255))
    telegram_user_id: Mapped[int | None]
    chat_id: Mapped[int | None]
    case_id: Mapped[int | None] = mapped_column(ForeignKey("case.id", ondelete="SET NULL"))
    #: The client the request was about.
    client_account: Mapped[str | None] = mapped_column(String(255))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
