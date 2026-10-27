import datetime

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from internal.access.guard import AccessGuard
from internal.access.oidc import GrantRejected, OidcProvider, Pending, ProviderUnavailable, Tokens
from internal.bot.outbox import Outbox
from internal.crypto import TokenCipher
from internal.entity import DeviceLogin, JournalKind
from internal.repository import DeviceLoginRepository, Journal, StaffRepository
from internal.words import Words

#: How much longer to wait between questions each time the provider asks to slow down (RFC 8628, 3.5).
SLOW_DOWN_SECONDS = 5


def _minutes_left(until: datetime.datetime, now: datetime.datetime) -> int:
    return max(1, int((until - now).total_seconds() // 60))


class DeviceLoginProcess:
    """Binds a Telegram account by the provider's device code: a Process Manager.

    A private message from an unbound account starts a sign-in, or reminds of the one under
    way; ``poll_due`` asks the provider about every sign-in whose turn has come, at the
    interval the provider set, and finishes it: a binding, a refusal, or an expired code.
    """

    def __init__(
        self,
        provider: OidcProvider,
        guard: AccessGuard,
        cipher: TokenCipher,
        words: Words,
        groups: frozenset[int],
        role_support: str,
    ) -> None:
        self._provider = provider
        self._guard = guard
        self._cipher = cipher
        self._words = words
        self._groups = groups
        self._role_support = role_support

    async def on_private_message(
        self,
        session: AsyncSession,
        outbox: Outbox,
        telegram_user_id: int,
        chat_id: int,
        telegram_username: str | None,
        now: datetime.datetime,
    ) -> None:
        logins = DeviceLoginRepository(session)
        login = await logins.of(telegram_user_id)
        if login is not None and login.expires_at > now:
            outbox.say(chat_id, self._words.say("login_pending", minutes=_minutes_left(login.expires_at, now)))
            return
        if login is not None:
            await logins.drop(login)

        try:
            code = await self._provider.start_device_login()
        except ProviderUnavailable:
            outbox.say(chat_id, self._words.say("provider_unavailable"))
            return
        expires_at = now + datetime.timedelta(seconds=code.expires_in)
        session.add(
            DeviceLogin(
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
                telegram_username=telegram_username,
                device_code=self._cipher.seal(code.device_code),
                verification_uri_complete=code.verification_uri_complete,
                interval_seconds=code.interval,
                next_poll_at=now + datetime.timedelta(seconds=code.interval),
                expires_at=expires_at,
            )
        )
        await session.flush()
        outbox.say(
            chat_id,
            self._words.say(
                "login_link",
                link=code.verification_uri_complete,
                code=code.user_code,
                minutes=_minutes_left(expires_at, now),
            ),
        )

    async def poll_due(self, session: AsyncSession, outbox: Outbox, now: datetime.datetime) -> None:
        logins = DeviceLoginRepository(session)
        for login in await logins.due(now):
            if login.expires_at <= now:
                await logins.drop(login)
                outbox.say(login.chat_id, self._words.say("login_expired"))
                continue
            try:
                result = await self._provider.poll_device_login(self._cipher.open(login.device_code))
            except ProviderUnavailable:
                # Asked again at the next turn; the code expires on its own if the provider stays away.
                login.next_poll_at = now + datetime.timedelta(seconds=login.interval_seconds)
                continue
            except GrantRejected as rejected:
                await logins.drop(login)
                key = "login_expired" if str(rejected) == "expired_token" else "login_declined"
                outbox.say(login.chat_id, self._words.say(key))
                continue

            if isinstance(result, Pending):
                if result.slow_down:
                    login.interval_seconds += SLOW_DOWN_SECONDS
                login.next_poll_at = now + datetime.timedelta(seconds=login.interval_seconds)
                continue

            await logins.drop(login)
            await self._finish(session, outbox, login, result)

    async def _finish(self, session: AsyncSession, outbox: Outbox, login: DeviceLogin, tokens: Tokens) -> None:
        identity = tokens.identity
        if self._role_support not in identity.roles:
            outbox.say(login.chat_id, self._words.say("login_no_role", account=identity.account_name))
            await Journal(session).write(
                JournalKind.REFUSED, subject=identity.subject, telegram_user_id=login.telegram_user_id, reason="no_role"
            )
            return

        replaced = await StaffRepository(session).bind(
            telegram_user_id=login.telegram_user_id,
            subject=identity.subject,
            account_name=identity.account_name,
            offline_token=self._cipher.seal(tokens.offline_token),
            # So that a mention of a responsible person is recognised before they write to the bot again.
            telegram_username=login.telegram_username,
        )
        journal = Journal(session)
        for old in replaced:
            await journal.write(
                JournalKind.UNBOUND, subject=old.subject, telegram_user_id=old.telegram_user_id, reason="replaced"
            )
            if old.telegram_user_id != login.telegram_user_id:
                outbox.say(old.telegram_user_id, self._words.say("unbound_replaced", account=old.account_name))
        await journal.write(JournalKind.BOUND, subject=identity.subject, telegram_user_id=login.telegram_user_id)
        logger.info("Telegram account bound to a provider account")

        outbox.say(login.chat_id, self._words.say("bound", account=identity.account_name))
        notice = self._words.say("binding_notice", telegram=login.telegram_user_id, account=identity.account_name)
        if self._groups:
            for group in sorted(self._groups):
                outbox.say(group, notice)
            return
        try:
            owners = await self._guard.owners()
        except ProviderUnavailable:
            logger.warning("The notice about a new binding could not reach the responsible people")
            return
        for owner in owners:
            outbox.say(owner.binding.telegram_user_id, notice)
