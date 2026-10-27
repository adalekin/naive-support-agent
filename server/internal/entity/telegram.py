from sqlalchemy.orm import Mapped, mapped_column

from internal.entity.base import Base


class TelegramOffset(Base):
    """The number of the first update not handled yet: an update is never handled twice."""

    __tablename__ = "telegram_offset"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False, default=1)
    next_update_id: Mapped[int]
