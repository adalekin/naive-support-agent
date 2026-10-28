"""Stand-ins the tests share: a provider that speaks OpenID Connect over pytest-httpx."""

import base64
import json
from urllib.parse import parse_qs

import httpx

from internal.access.oidc import Role

ISSUER = "https://idp.test/realms/support"
TOKEN_URL = f"{ISSUER}/protocol/openid-connect/token"
DEVICE_URL = f"{ISSUER}/protocol/openid-connect/auth/device"
#: Access, responsible person, aggregate questions: the last is a role of the admin panel's client.
ROLES = (
    Role("resource_access.support-assistant.roles", "support"),
    Role("resource_access.support-assistant.roles", "owner"),
    Role("resource_access.product-admin.roles", "admin"),
)
CLAIMS = ["resource_access.support-assistant.roles", "resource_access.product-admin.roles"]


def jwt(claims: dict) -> str:
    def part(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'RS256'})}.{part(claims)}.c2ln"


def access_token(sub: str, name: str, roles: list[str], admin_roles: list[str] | None = None) -> str:
    access = {"support-assistant": {"roles": roles}}
    if admin_roles:
        access["product-admin"] = {"roles": admin_roles}
    return jwt({"sub": sub, "preferred_username": name, "resource_access": access})


class FakeProvider:
    """Accounts with roles; offline tokens rotate on every refresh, as with Revoke Refresh Token on."""

    def __init__(self, httpx_mock) -> None:
        self.accounts: dict[str, dict] = {}
        self.tokens: dict[str, str] = {}
        self.devices: dict[str, dict] = {}
        self.down = False
        self._serial = 0
        httpx_mock.add_callback(
            self._discovery, url=f"{ISSUER}/.well-known/openid-configuration", is_reusable=True, is_optional=True
        )
        httpx_mock.add_callback(self._token, url=TOKEN_URL, is_reusable=True, is_optional=True)
        httpx_mock.add_callback(self._device, url=DEVICE_URL, is_reusable=True, is_optional=True)

    def account(self, sub: str, name: str, roles: list[str], admin_roles: list[str] | None = None) -> None:
        self.accounts[sub] = {"name": name, "roles": roles, "admin_roles": admin_roles, "enabled": True}

    def issue(self, sub: str) -> str:
        self._serial += 1
        token = f"offline-{sub}-{self._serial}"
        self.tokens[token] = sub
        return token

    def _discovery(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(503)
        return httpx.Response(
            200, json={"issuer": ISSUER, "token_endpoint": TOKEN_URL, "device_authorization_endpoint": DEVICE_URL}
        )

    def _grant(self, sub: str) -> httpx.Response:
        account = self.accounts[sub]
        return httpx.Response(
            200,
            json={
                "access_token": access_token(sub, account["name"], account["roles"], account["admin_roles"]),
                "refresh_token": self.issue(sub),
            },
        )

    def _token(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(503)
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        if form.get("client_secret") != "client-secret":
            return httpx.Response(401, json={"error": "invalid_client"})
        if form["grant_type"] == "refresh_token":
            sub = self.tokens.pop(form["refresh_token"], None)
            if sub is None or not self.accounts[sub]["enabled"]:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "User disabled"})
            return self._grant(sub)
        device = self.devices.get(form["device_code"])
        if device is None:
            return httpx.Response(400, json={"error": "expired_token"})
        if device["state"] == "pending":
            return httpx.Response(400, json={"error": "authorization_pending"})
        if device["state"] == "declined":
            return httpx.Response(400, json={"error": "access_denied"})
        del self.devices[form["device_code"]]
        return self._grant(device["sub"])

    def _device(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(503)
        self._serial += 1
        code = f"device-{self._serial}"
        self.devices[code] = {"state": "pending", "sub": None}
        return httpx.Response(
            200,
            json={
                "device_code": code,
                "user_code": f"USER-{self._serial}",
                "verification_uri": f"{ISSUER}/device",
                "verification_uri_complete": f"{ISSUER}/device?user_code=USER-{self._serial}",
                "expires_in": 600,
                "interval": 5,
            },
        )

    def confirm(self, device_code: str, sub: str) -> None:
        self.devices[device_code] = {"state": "confirmed", "sub": sub}
