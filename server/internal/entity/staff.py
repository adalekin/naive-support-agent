import datetime

from sqlalchemy import DateTime, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from internal.entity.base import Base, now_column


class StaffBinding(Base):
    """A Telegram account bound to an account at the provider: the only way to the assistant.

    The Telegram account is known by its number alone; its user name is kept only to recognise
    a mention of a responsible person, never to decide who may use the assistant.
    """

    __tablename__ = "staff_binding"

    telegram_user_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    #: ``sub`` of the account at the provider. One Telegram account per provider account.
    subject: Mapped[str] = mapped_column(String(255), unique=True)
    #: How people know the account at the provider: shown in the notice about the binding and in the journal.
    account_name: Mapped[str] = mapped_column(String(255))
    #: The offline token, encrypted with the installation's key.
    offline_token: Mapped[bytes] = mapped_column(LargeBinary)
    #: The last user name the account wrote under, for mentions only.
    telegram_username: Mapped[str | None] = mapped_column(String(64))
    bound_at: Mapped[datetime.datetime] = now_column()


class DeviceLogin(Base):
    """A sign-in in progress for a Telegram account that is not bound yet: one at a time per account."""

    __tablename__ = "device_login"

    telegram_user_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    #: The private chat the link was sent to and the outcome is told in.
    chat_id: Mapped[int]
    #: The user name the account wrote under, kept on the binding for mentions only.
    telegram_username: Mapped[str | None] = mapped_column(String(64))
    #: The device code, encrypted: with it and the client secret anyone could collect the tokens.
    device_code: Mapped[bytes] = mapped_column(LargeBinary)
    verification_uri_complete: Mapped[str] = mapped_column(String(1024))
    #: How often the provider allows asking about this code.
    interval_seconds: Mapped[int]
    next_poll_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
