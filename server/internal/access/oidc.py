"""The installation's OpenID Connect provider, as the assistant uses it.

Only what OpenID Connect and RFC 8628 define: addresses come from the discovery document,
and nothing here knows which product the provider is.
"""

import asyncio
import base64
import json
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger

HTTP_TIMEOUT_SECONDS = 15.0
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class ProviderUnavailable(Exception):
    """The provider did not answer, or answered something the assistant cannot use. Try later."""


class GrantRejected(Exception):
    """The provider refused the token: the account is disabled, the session revoked, the code dead."""


@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    #: The page of the provider with the code already filled in.
    verification_uri_complete: str
    expires_in: int
    interval: int


@dataclass(frozen=True)
class Identity:
    """Who the tokens belong to and what they may do, read from the access token."""

    subject: str
    account_name: str
    roles: frozenset[str]


@dataclass(frozen=True)
class Tokens:
    identity: Identity
    #: The offline token to keep: a new one when the provider rotates them, else the one used.
    offline_token: str


class Pending:
    """The person has not confirmed the code yet."""

    def __init__(self, slow_down: bool) -> None:
        #: The provider asks to wait longer between questions.
        self.slow_down = slow_down


class OidcProvider:
    def __init__(self, issuer: str, client_id: str, client_secret: str, roles_claim: str) -> None:
        self._issuer = issuer
        self._client = {"client_id": client_id, "client_secret": client_secret}
        self._roles_path = roles_claim.split(".")
        self._configuration: dict[str, Any] | None = None
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS)

    async def close(self) -> None:
        await self._http.aclose()

    async def start_device_login(self) -> DeviceCode:
        endpoint = await self._endpoint("device_authorization_endpoint")
        body = await self._post(endpoint, {"scope": "openid offline_access"})
        try:
            return DeviceCode(
                device_code=body["device_code"],
                user_code=body["user_code"],
                verification_uri_complete=body["verification_uri_complete"],
                expires_in=int(body["expires_in"]),
                interval=int(body.get("interval", 5)),
            )
        except (KeyError, ValueError) as exc:
            raise ProviderUnavailable("The device authorization response is incomplete") from exc

    async def poll_device_login(self, device_code: str) -> Tokens | Pending:
        """Raises ``GrantRejected`` when the code expired or the person declined it."""
        endpoint = await self._endpoint("token_endpoint")
        try:
            body = await self._post(endpoint, {"grant_type": DEVICE_GRANT, "device_code": device_code})
        except _OAuthError as error:
            if error.code == "authorization_pending":
                return Pending(slow_down=False)
            if error.code == "slow_down":
                return Pending(slow_down=True)
            if error.code in ("expired_token", "access_denied", "invalid_grant"):
                raise GrantRejected(error.code) from None
            raise ProviderUnavailable(f"The provider refused the device code: {error.code}") from None
        return self._tokens(body, fallback_refresh=None)

    async def refresh(self, offline_token: str) -> Tokens:
        """Exchange the offline token. Raises ``GrantRejected`` when the provider refuses it."""
        endpoint = await self._endpoint("token_endpoint")
        try:
            body = await self._post(endpoint, {"grant_type": "refresh_token", "refresh_token": offline_token})
        except _OAuthError as error:
            # Only a refused grant says the account lost access. A refused client is the
            # installation's mistake: it must not unbind the staff.
            if error.code == "invalid_grant":
                raise GrantRejected(error.code) from None
            raise ProviderUnavailable(f"The provider refused the refresh: {error.code}") from None
        return self._tokens(body, fallback_refresh=offline_token)

    def _tokens(self, body: dict[str, Any], fallback_refresh: str | None) -> Tokens:
        refresh_token = body.get("refresh_token") or fallback_refresh
        if "access_token" not in body or refresh_token is None:
            raise ProviderUnavailable("The token response is incomplete")
        return Tokens(identity=self._identity(body["access_token"]), offline_token=refresh_token)

    def _identity(self, access_token: str) -> Identity:
        # The token came straight from the provider's token endpoint over TLS, in answer to a
        # request the assistant authenticated with its client secret: its claims are read as they are.
        try:
            payload = access_token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except (IndexError, ValueError) as exc:
            raise ProviderUnavailable("The access token is not a JWT") from exc

        roles: Any = claims
        for part in self._roles_path:
            roles = roles.get(part, {}) if isinstance(roles, dict) else {}
        if "sub" not in claims:
            raise ProviderUnavailable("The access token has no subject")
        return Identity(
            subject=claims["sub"],
            account_name=claims.get("preferred_username") or claims.get("email") or claims["sub"],
            roles=frozenset(role for role in roles if isinstance(role, str))
            if isinstance(roles, list)
            else frozenset(),
        )

    async def _endpoint(self, name: str) -> str:
        if self._configuration is None:
            async with self._lock:
                if self._configuration is None:
                    try:
                        response = await self._http.get(f"{self._issuer}/.well-known/openid-configuration")
                        response.raise_for_status()
                        self._configuration = response.json()
                    except (httpx.HTTPError, ValueError) as exc:
                        logger.warning("Identity provider discovery failed: {}", type(exc).__name__)
                        raise ProviderUnavailable("The identity provider is unavailable") from exc
        try:
            return self._configuration[name]
        except KeyError as exc:
            raise ProviderUnavailable(f"The provider does not publish {name}") from exc

    async def _post(self, url: str, form: dict[str, str]) -> dict[str, Any]:
        try:
            response = await self._http.post(url, data={**self._client, **form})
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Identity provider request failed: {}", type(exc).__name__)
            raise ProviderUnavailable("The identity provider is unavailable") from exc
        if response.status_code == 200:
            return body
        if response.status_code in (400, 401) and isinstance(body, dict) and "error" in body:
            raise _OAuthError(str(body["error"]))
        logger.warning("Identity provider answered {}", response.status_code)
        raise ProviderUnavailable(f"The identity provider answered {response.status_code}")


class _OAuthError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
