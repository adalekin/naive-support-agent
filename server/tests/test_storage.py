import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text

from internal.crypto import TokenCipher
from internal.entity import Base
from internal.repository import StaffRepository
from tests.conftest import TEST_DATABASE_URL, TEST_ENVIRONMENT, alembic_config, rebuild_schema


def table_names() -> set[str]:
    engine = create_engine(TEST_DATABASE_URL.set(drivername="postgresql+psycopg"))
    with engine.connect() as connection:
        names = set(inspect(connection).get_table_names())
    engine.dispose()
    return names - {"alembic_version"}


def test_migration_applies_on_an_empty_database_and_rolls_back(migrated_schema):
    assert table_names() == set(Base.metadata.tables)
    command.downgrade(alembic_config(), "base")
    assert table_names() == set()
    rebuild_schema()
    assert table_names() == set(Base.metadata.tables)


cipher = TokenCipher(TEST_ENVIRONMENT["ENCRYPTION_KEY"])


async def test_one_binding_per_provider_account(database):
    async with database.unit_of_work() as session:
        await StaffRepository(session).bind(1, "sub-anna", "anna", cipher.seal("old"), "anna_tg")
    async with database.unit_of_work() as session:
        replaced = await StaffRepository(session).bind(2, "sub-anna", "anna", cipher.seal("new"), "anna_new")
        assert [binding.telegram_user_id for binding in replaced] == [1]
    async with database.unit_of_work() as session:
        staff = StaffRepository(session)
        assert await staff.by_telegram(1) is None
        assert (await staff.by_telegram(2)).subject == "sub-anna"


async def test_one_binding_per_telegram_account(database):
    async with database.unit_of_work() as session:
        await StaffRepository(session).bind(1, "sub-anna", "anna", cipher.seal("a"), None)
    async with database.unit_of_work() as session:
        await StaffRepository(session).bind(1, "sub-oleg", "oleg", cipher.seal("o"), None)
    async with database.unit_of_work() as session:
        assert [binding.subject for binding in await StaffRepository(session).all()] == ["sub-oleg"]


async def test_offline_token_is_not_stored_in_the_clear(database):
    token = "eyJhbGciOiJIUzI1NiJ9.offline-token-of-anna.signature"
    async with database.unit_of_work() as session:
        await StaffRepository(session).bind(1, "sub-anna", "anna", cipher.seal(token), None)
    async with database.engine.connect() as connection:
        stored = (await connection.execute(text("SELECT offline_token FROM staff_binding"))).scalar_one()
    assert token.encode() not in stored
    assert cipher.open(stored) == token


def test_a_changed_key_cannot_open_old_tokens():
    sealed = cipher.seal("secret")
    other = TokenCipher("Zm9vYmFyYmF6cXV4cXV1eGZvb2JhcmJhenF1eHF1dXg=")
    with pytest.raises(Exception) as error:
        other.open(sealed)
    assert type(error.value).__name__ == "InvalidToken"


async def test_the_data_of_a_case_is_not_stored_in_the_clear(database):
    from internal.entity import TurnKind
    from internal.repository import CaseRepository

    async with database.unit_of_work() as session:
        cases = CaseRepository(session)
        case = await cases.open(-1001, 10, False, "sub-anna")
        case.labels = {"<<email_1>>": "client@example.com"}
        case.conversation = [{"role": "user", "content": "Иван Петров, client@example.com"}]
        await cases.enqueue(case, TurnKind.REQUEST, {"text": "Иван Петров пишет с client@example.com"})
    async with database.engine.connect() as connection:
        row = (
            await connection.execute(
                text('SELECT c.labels, c.conversation, t.payload FROM "case" c JOIN case_turn t ON t.case_id = c.id')
            )
        ).one()
    for stored in row:
        assert b"client@example.com" not in stored and "Иван".encode() not in stored
    async with database.unit_of_work() as session:
        case = await CaseRepository(session).get(case.id)
        assert case.labels == {"<<email_1>>": "client@example.com"}
