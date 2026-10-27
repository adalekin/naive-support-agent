import datetime

from sqlalchemy import select, update

from internal.bot.retention import RetentionSweeper
from internal.entity import Case, CaseMessage, CaseTurn, JournalEntry, JournalKind, TurnKind
from internal.repository import CaseRepository, Journal

NOW = datetime.datetime(2026, 12, 1, tzinfo=datetime.UTC)


async def case_last_touched(database, root, days_ago):
    async with database.unit_of_work() as session:
        cases = CaseRepository(session)
        case = await cases.open(-1001, root, False, "sub-anna")
        await cases.enqueue(case, TurnKind.REQUEST, {"text": "client@example.com"})
        await Journal(session).write(JournalKind.REQUEST, subject="sub-anna", case_id=case.id, client_account="acc-7")
        await session.execute(
            update(Case).where(Case.id == case.id).values(updated_at=NOW - datetime.timedelta(days=days_ago))
        )
        return case.id


async def test_a_case_untouched_past_the_period_goes_and_the_journal_stays(database):
    old = await case_last_touched(database, 10, 31)
    recent = await case_last_touched(database, 11, 29)
    assert await RetentionSweeper(database, 30).sweep(NOW) == 1
    async with database.unit_of_work() as session:
        assert list(await session.scalars(select(Case.id))) == [recent]
        assert await session.scalar(select(CaseTurn).where(CaseTurn.case_id == old)) is None
        assert await session.scalar(select(CaseMessage).where(CaseMessage.case_id == old)) is None
        journal = list(await session.scalars(select(JournalEntry).order_by(JournalEntry.id)))
    assert [(entry.case_id, entry.client_account) for entry in journal] == [(None, "acc-7"), (recent, "acc-7")]


async def test_a_reply_in_the_thread_of_a_deleted_case_starts_a_new_one(database):
    await case_last_touched(database, 10, 31)
    await RetentionSweeper(database, 30).sweep(NOW)
    async with database.unit_of_work() as session:
        assert await CaseRepository(session).of_message(-1001, 10) is None
