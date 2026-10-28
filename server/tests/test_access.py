import asyncio
import datetime

import pytest
from sqlalchemy import select

from internal.access.guard import AccessGuard
from internal.access.login import DeviceLoginProcess
from internal.access.oidc import OidcProvider, ProviderUnavailable
from internal.bot.outbox import Outbox
from internal.crypto import TokenCipher
from internal.entity import DeviceLogin, JournalEntry, JournalKind
from internal.repository import StaffRepository
from internal.words import Words
from tests.conftest import TEST_ENVIRONMENT
from tests.fakes import CLAIMS, ISSUER, ROLES, FakeProvider

NOW = datetime.datetime(2026, 10, 27, 12, 0, tzinfo=datetime.UTC)
cipher = TokenCipher(TEST_ENVIRONMENT["ENCRYPTION_KEY"])


@pytest.fixture
def provider(httpx_mock):
    fake = FakeProvider(httpx_mock)
    fake.account("sub-anna", "anna", ["support"])
    fake.account("sub-oleg", "oleg", ["support", "owner"])
    fake.account("sub-eve", "eve", [])
    return fake


@pytest.fixture
def oidc():
    return OidcProvider(ISSUER, "support-assistant", "client-secret", CLAIMS)


@pytest.fixture
def guard(database, oidc):
    return AccessGuard(database, oidc, cipher, *ROLES)


def login_process(oidc, guard, groups=frozenset({-1001})):
    return DeviceLoginProcess(oidc, guard, cipher, Words("en"), groups, ROLES[0])


async def bind(database, provider, telegram_user_id, sub):
    async with database.unit_of_work() as session:
        name = provider.accounts[sub]["name"]
        await StaffRepository(session).bind(telegram_user_id, sub, name, cipher.seal(provider.issue(sub)), None)


async def check(database, guard, telegram_user_id):
    return await guard.check(telegram_user_id)


async def journal(database, kind):
    async with database.unit_of_work() as session:
        return list(await session.scalars(select(JournalEntry).where(JournalEntry.kind == kind)))


async def test_staff_with_the_role_may_use_the_assistant(database, provider, guard):
    await bind(database, provider, 10, "sub-anna")
    staff = await check(database, guard, 10)
    assert staff.may_use and not staff.is_owner


async def test_role_removed_means_no_use_on_the_next_request(database, provider, guard):
    await bind(database, provider, 10, "sub-anna")
    provider.accounts["sub-anna"]["roles"] = []
    staff = await check(database, guard, 10)
    assert staff is not None and not staff.may_use


async def test_disabled_account_loses_its_binding(database, provider, guard):
    await bind(database, provider, 10, "sub-anna")
    provider.accounts["sub-anna"]["enabled"] = False
    assert await check(database, guard, 10) is None
    async with database.unit_of_work() as session:
        assert await StaffRepository(session).by_telegram(10) is None
    assert [entry.details["reason"] for entry in await journal(database, JournalKind.UNBOUND)] == ["invalid_grant"]


async def test_provider_down_is_not_a_refusal(database, provider, guard):
    await bind(database, provider, 10, "sub-anna")
    provider.down = True
    with pytest.raises(ProviderUnavailable):
        await check(database, guard, 10)
    provider.down = False
    assert (await check(database, guard, 10)).may_use


async def test_misconfigured_client_does_not_unbind_the_staff(database, provider, httpx_mock):
    await bind(database, provider, 10, "sub-anna")
    wrong = AccessGuard(
        database,
        OidcProvider(ISSUER, "support-assistant", "wrong", CLAIMS),
        cipher,
        *ROLES,
    )
    with pytest.raises(ProviderUnavailable):
        await check(database, wrong, 10)
    async with database.unit_of_work() as session:
        assert await StaffRepository(session).by_telegram(10) is not None


async def test_two_requests_of_one_person_do_not_spend_one_token(database, provider, guard):
    await bind(database, provider, 10, "sub-anna")
    first, second = await asyncio.gather(check(database, guard, 10), check(database, guard, 10))
    assert first.may_use and second.may_use


async def test_unbound_person_gets_one_link_at_a_time(database, provider, oidc, guard):
    process = login_process(oidc, guard)
    outbox = Outbox()
    async with database.unit_of_work() as session:
        await process.on_private_message(session, outbox, 20, 20, None, NOW)
    async with database.unit_of_work() as session:
        await process.on_private_message(session, outbox, 20, 20, None, NOW + datetime.timedelta(minutes=1))
    assert "USER-" in outbox.replies[0].text.text
    assert "link is above" in outbox.replies[1].text.text
    assert len(provider.devices) == 1


async def test_staff_binds_telegram_and_the_groups_are_told(database, provider, oidc, guard):
    process = login_process(oidc, guard)
    outbox = Outbox()
    async with database.unit_of_work() as session:
        await process.on_private_message(session, outbox, 20, 20, None, NOW)
    provider.confirm(next(iter(provider.devices)), "sub-anna")
    outbox = Outbox()
    async with database.unit_of_work() as session:
        await process.poll_due(session, outbox, NOW + datetime.timedelta(seconds=6))
    said = {(reply.chat_id, reply.text.text) for reply in outbox.replies}
    assert (20, "Telegram is bound to the account anna. Send your cases.") in said
    assert (-1001, "New binding: Telegram 20 is bound to the account anna.") in said
    assert (await check(database, guard, 20)).may_use
    assert len(await journal(database, JournalKind.BOUND)) == 1


async def test_without_groups_the_notice_goes_to_the_owners(database, provider, oidc, guard):
    await bind(database, provider, 30, "sub-oleg")
    process = login_process(oidc, guard, groups=frozenset())
    async with database.unit_of_work() as session:
        await process.on_private_message(session, Outbox(), 20, 20, None, NOW)
    provider.confirm(next(iter(provider.devices)), "sub-anna")
    outbox = Outbox()
    async with database.unit_of_work() as session:
        await process.poll_due(session, outbox, NOW + datetime.timedelta(seconds=6))
    assert [reply.chat_id for reply in outbox.replies if "New binding" in reply.text.text] == [30]


async def test_sign_in_without_the_role_binds_nothing(database, provider, oidc, guard):
    process = login_process(oidc, guard)
    async with database.unit_of_work() as session:
        await process.on_private_message(session, Outbox(), 20, 20, None, NOW)
    provider.confirm(next(iter(provider.devices)), "sub-eve")
    outbox = Outbox()
    async with database.unit_of_work() as session:
        await process.poll_due(session, outbox, NOW + datetime.timedelta(seconds=6))
    assert "has no access" in outbox.replies[0].text.text
    assert await check(database, guard, 20) is None


async def test_expired_code_binds_nothing_and_the_next_message_gets_a_new_link(database, provider, oidc, guard):
    process = login_process(oidc, guard)
    async with database.unit_of_work() as session:
        await process.on_private_message(session, Outbox(), 20, 20, None, NOW)
    first_code = next(iter(provider.devices))
    later = NOW + datetime.timedelta(minutes=11)
    outbox = Outbox()
    async with database.unit_of_work() as session:
        await process.poll_due(session, outbox, later)
    assert "expired" in outbox.replies[0].text.text
    provider.confirm(first_code, "sub-anna")
    outbox = Outbox()
    async with database.unit_of_work() as session:
        await process.on_private_message(session, outbox, 20, 20, None, later)
        assert (await session.scalar(select(DeviceLogin))) is not None
    assert "USER-" in outbox.replies[0].text.text
    assert await check(database, guard, 20) is None


async def test_new_telegram_account_replaces_the_old_one(database, provider, oidc, guard):
    await bind(database, provider, 10, "sub-anna")
    process = login_process(oidc, guard)
    async with database.unit_of_work() as session:
        await process.on_private_message(session, Outbox(), 11, 11, None, NOW)
    provider.confirm(next(iter(provider.devices)), "sub-anna")
    outbox = Outbox()
    async with database.unit_of_work() as session:
        await process.poll_due(session, outbox, NOW + datetime.timedelta(seconds=6))
    assert await check(database, guard, 10) is None
    assert (await check(database, guard, 11)).may_use
    assert any(reply.chat_id == 10 for reply in outbox.replies)


async def test_a_rotated_token_is_kept_even_if_the_request_rolls_back(database, provider, guard):
    await bind(database, provider, 10, "sub-anna")

    class Rollback(Exception):
        pass

    with pytest.raises(Rollback):
        async with database.unit_of_work():
            assert (await guard.check(10)).may_use
            raise Rollback
    # The old token is dead at the provider; the saved one still works.
    assert (await check(database, guard, 10)).may_use


async def test_the_role_for_aggregate_questions_is_read_from_another_application(database, provider, guard):
    provider.account("sub-boss", "boss", ["support"], admin_roles=["admin"])
    await bind(database, provider, 40, "sub-boss")
    await bind(database, provider, 10, "sub-anna")
    assert (await check(database, guard, 40)).may_analyze
    assert not (await check(database, guard, 10)).may_analyze
