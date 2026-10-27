"""Repositories over the assistant's own tables. Each works inside the unit of work it is given."""

import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from internal.entity import (
    Case,
    CaseMessage,
    CaseState,
    CaseTurn,
    DeviceLogin,
    Incident,
    JournalEntry,
    JournalKind,
    MessageRole,
    StaffBinding,
    TelegramOffset,
    TurnKind,
)


class StaffRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def by_telegram(self, telegram_user_id: int) -> StaffBinding | None:
        return await self.session.get(StaffBinding, telegram_user_id)

    async def by_username(self, username: str) -> StaffBinding | None:
        return await self.session.scalar(
            select(StaffBinding).where(func.lower(StaffBinding.telegram_username) == username.lower())
        )

    async def all(self) -> list[StaffBinding]:
        return list(await self.session.scalars(select(StaffBinding).order_by(StaffBinding.bound_at)))

    async def bind(
        self,
        telegram_user_id: int,
        subject: str,
        account_name: str,
        offline_token: bytes,
        telegram_username: str | None,
    ) -> list[StaffBinding]:
        """Bind the Telegram account to the provider account. Returns the bindings it replaced.

        Both sides are unique: a provider account keeps one Telegram account, and a Telegram
        account is bound to one provider account.
        """
        replaced = list(
            await self.session.scalars(
                select(StaffBinding).where(
                    (StaffBinding.subject == subject) | (StaffBinding.telegram_user_id == telegram_user_id)
                )
            )
        )
        for binding in replaced:
            await self.session.delete(binding)
        await self.session.flush()
        self.session.add(
            StaffBinding(
                telegram_user_id=telegram_user_id,
                subject=subject,
                account_name=account_name,
                offline_token=offline_token,
                telegram_username=telegram_username,
            )
        )
        await self.session.flush()
        return replaced

    async def unbind(self, binding: StaffBinding) -> None:
        await self.session.delete(binding)
        await self.session.flush()


class DeviceLoginRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def of(self, telegram_user_id: int) -> DeviceLogin | None:
        return await self.session.get(DeviceLogin, telegram_user_id)

    async def due(self, now: datetime.datetime) -> list[DeviceLogin]:
        return list(
            await self.session.scalars(
                select(DeviceLogin).where(DeviceLogin.next_poll_at <= now).with_for_update(skip_locked=True)
            )
        )

    async def drop(self, login: DeviceLogin) -> None:
        await self.session.delete(login)
        await self.session.flush()


class CaseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, case_id: int, *, for_update: bool = False) -> Case | None:
        return await self.session.get(Case, case_id, with_for_update=for_update)

    async def of_message(self, chat_id: int, message_id: int) -> Case | None:
        """The case a message belongs to: a reply to it continues that case."""
        return await self.session.scalar(
            select(Case)
            .join(CaseMessage, CaseMessage.case_id == Case.id)
            .where(CaseMessage.chat_id == chat_id, CaseMessage.message_id == message_id)
        )

    async def open(self, chat_id: int, root_message_id: int, private: bool, staff_subject: str) -> Case:
        case = Case(
            chat_id=chat_id,
            root_message_id=root_message_id,
            private=private,
            staff_subject=staff_subject,
            state=CaseState.INVESTIGATING,
            conversation=[],
            labels={},
        )
        self.session.add(case)
        await self.session.flush()
        await self.remember_message(case.chat_id, root_message_id, case.id, MessageRole.REQUEST)
        return case

    async def remember_message(self, chat_id: int, message_id: int, case_id: int, role: MessageRole) -> None:
        await self.session.execute(
            insert(CaseMessage)
            .values(chat_id=chat_id, message_id=message_id, case_id=case_id, role=role)
            .on_conflict_do_nothing()
        )

    async def message(self, chat_id: int, message_id: int) -> CaseMessage | None:
        return await self.session.get(CaseMessage, (chat_id, message_id))

    async def enqueue(self, case: Case, kind: TurnKind, payload: dict[str, Any]) -> CaseTurn:
        turn = CaseTurn(case_id=case.id, kind=kind, payload=payload)
        self.session.add(turn)
        await self.session.flush()
        return turn

    async def pending_turns(self) -> list[CaseTurn]:
        return list(
            await self.session.scalars(select(CaseTurn).where(CaseTurn.done_at.is_(None)).order_by(CaseTurn.id))
        )

    async def has_pending_turn(self, case_id: int) -> bool:
        found = await self.session.scalar(
            select(CaseTurn.id).where(CaseTurn.case_id == case_id, CaseTurn.done_at.is_(None)).limit(1)
        )
        return found is not None

    async def open_of(self, staff_subject: str) -> list[Case]:
        return list(
            await self.session.scalars(
                select(Case)
                .where(Case.staff_subject == staff_subject, Case.state != CaseState.RESOLVED)
                .order_by(Case.created_at)
            )
        )

    async def waiting_for_fix(self) -> list[Case]:
        return list(
            await self.session.scalars(
                select(Case).where(Case.state == CaseState.WAITING_FIX, Case.owner_took_over.is_(False))
            )
        )


class IncidentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, incident_id: int) -> Incident | None:
        return await self.session.get(Incident, incident_id, with_for_update=True)

    async def by_cause(self, cause: str) -> Incident | None:
        return await self.session.scalar(select(Incident).where(Incident.cause == cause).with_for_update())

    async def open_ones(self) -> list[Incident]:
        return list(await self.session.scalars(select(Incident).where(Incident.resolved_at.is_(None))))

    async def add(self, cause: str, description: str) -> Incident:
        incident = Incident(cause=cause, description=description)
        self.session.add(incident)
        await self.session.flush()
        return incident


class Journal:
    """Appends to the journal; nothing here changes or removes an entry."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def write(
        self,
        kind: JournalKind,
        *,
        subject: str | None = None,
        telegram_user_id: int | None = None,
        chat_id: int | None = None,
        case_id: int | None = None,
        client_account: str | None = None,
        **details: Any,
    ) -> None:
        self.session.add(
            JournalEntry(
                kind=kind,
                subject=subject,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
                case_id=case_id,
                client_account=client_account,
                details=details,
            )
        )
        await self.session.flush()

    async def since(self, moment: datetime.datetime) -> list[JournalEntry]:
        return list(
            await self.session.scalars(select(JournalEntry).where(JournalEntry.at >= moment).order_by(JournalEntry.id))
        )


class OffsetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def next_update_id(self) -> int | None:
        offset = await self.session.get(TelegramOffset, 1, with_for_update=True)
        return offset.next_update_id if offset else None

    async def move_to(self, next_update_id: int) -> None:
        await self.session.execute(
            insert(TelegramOffset)
            .values(id=1, next_update_id=next_update_id)
            .on_conflict_do_update(index_elements=["id"], set_={"next_update_id": next_update_id})
        )


async def forget_logins_of(session: AsyncSession, telegram_user_id: int) -> None:
    await session.execute(delete(DeviceLogin).where(DeviceLogin.telegram_user_id == telegram_user_id))
