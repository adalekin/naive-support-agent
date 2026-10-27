import asyncio
from collections import defaultdict
from dataclasses import dataclass

from loguru import logger
from sqlalchemy import delete, select, update

from internal.access.oidc import GrantRejected, Identity, OidcProvider
from internal.crypto import InvalidToken, TokenCipher
from internal.db import Database
from internal.entity import JournalKind, StaffBinding
from internal.repository import Journal


@dataclass(frozen=True)
class Staff:
    """A bound person as the provider sees them right now."""

    binding: StaffBinding
    identity: Identity
    may_use: bool
    is_owner: bool


class AccessGuard:
    """Asks the provider about a bound person on every request: no role is remembered between them.

    Exchanges of one person's offline token go one at a time, so two messages never spend the
    same token. A new token, when the provider rotates them, is saved in a transaction of its
    own at once: the old one is already dead, and no rollback of the request may lose the new
    one. A refused exchange removes the binding: the account was disabled or its session revoked.
    The service runs as one process (one reader of the bot's updates), so a lock in memory is enough.
    """

    def __init__(
        self, database: Database, provider: OidcProvider, cipher: TokenCipher, role_support: str, role_owner: str
    ) -> None:
        self._database = database
        self._provider = provider
        self._cipher = cipher
        self._role_support = role_support
        self._role_owner = role_owner
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def check(self, telegram_user_id: int) -> Staff | None:
        """None when the Telegram account is not bound or the binding was just removed.

        Raises ``ProviderUnavailable`` when the provider cannot say.
        """
        async with self._locks[telegram_user_id]:
            async with self._database.unit_of_work() as session:
                binding = await session.get(StaffBinding, telegram_user_id)
            if binding is None:
                return None
            return await self._check(binding)

    async def owners(self) -> list[Staff]:
        """The bound people who hold the role of a responsible person now."""
        async with self._database.unit_of_work() as session:
            bindings = list(await session.scalars(select(StaffBinding).order_by(StaffBinding.bound_at)))
        found = []
        for binding in bindings:
            async with self._locks[binding.telegram_user_id]:
                staff = await self._check(binding)
            if staff is not None and staff.is_owner:
                found.append(staff)
        return found

    async def _check(self, binding: StaffBinding) -> Staff | None:
        try:
            offline_token = self._cipher.open(binding.offline_token)
        except InvalidToken:
            logger.warning("The offline token of a binding cannot be opened with the current key; the binding goes")
            await self._unbind(binding, "key_changed")
            return None
        try:
            tokens = await self._provider.refresh(offline_token)
        except GrantRejected as rejected:
            await self._unbind(binding, str(rejected))
            return None

        identity = tokens.identity
        changes: dict[str, object] = {"account_name": identity.account_name}
        if tokens.offline_token != offline_token:
            changes["offline_token"] = self._cipher.seal(tokens.offline_token)
        async with self._database.unit_of_work() as session:
            await session.execute(update(StaffBinding).where(self._same(binding)).values(**changes))
        binding.account_name = identity.account_name
        if "offline_token" in changes:
            binding.offline_token = changes["offline_token"]  # type: ignore[assignment]
        return Staff(
            binding=binding,
            identity=identity,
            may_use=self._role_support in identity.roles,
            is_owner=self._role_owner in identity.roles,
        )

    async def _unbind(self, binding: StaffBinding, reason: str) -> None:
        async with self._database.unit_of_work() as session:
            await Journal(session).write(
                JournalKind.UNBOUND, subject=binding.subject, telegram_user_id=binding.telegram_user_id, reason=reason
            )
            await session.execute(delete(StaffBinding).where(self._same(binding)))

    @staticmethod
    def _same(binding: StaffBinding):
        # The same binding, not a newer one of the same Telegram account.
        return (StaffBinding.telegram_user_id == binding.telegram_user_id) & (StaffBinding.subject == binding.subject)
