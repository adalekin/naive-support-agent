from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy import URL

from internal.access.oidc import Role


class ConfigurationError(RuntimeError):
    """An installation setting is missing or invalid; the message names every such setting."""


class Settings(BaseSettings):
    """Installation settings, read from the environment only.

    None of them has a default: a value substituted silently for access or for spending is
    worse than a refused start. They are not read from a ``.env`` file, so that a file left
    in a checkout never reaches the tests or a container.
    """

    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    TELEGRAM_BOT_TOKEN: SecretStr
    #: The name of the bot without the at sign: a mention of the assistant is found by it.
    TELEGRAM_BOT_USERNAME: str = Field(min_length=1)
    #: How the service reaches the Telegram Bot API: straight or through the proxy of the installation.
    TELEGRAM_EGRESS: Literal["direct", "proxy"]
    TELEGRAM_PROXY_URL: SecretStr | None = None
    #: The language of the assistant's own messages: the sign-in, the notices, the buttons.
    #: The language of its answers about cases comes from the instructions.
    LOCALE: Literal["ru", "en"]
    #: Numbers of the internal group chats the assistant works in, comma separated. Empty: private chats only.
    TELEGRAM_GROUPS: Annotated[frozenset[int], NoDecode]

    OIDC_ISSUER: str = Field(min_length=1)
    OIDC_CLIENT_ID: str = Field(min_length=1)
    OIDC_CLIENT_SECRET: SecretStr
    #: Where the provider lists the roles in an access token, as a dotted path: ``resource_access.<client>.roles``.
    OIDC_ROLES_CLAIM: str = Field(min_length=1)
    #: The role that lets a person use the assistant.
    ROLE_SUPPORT: str = Field(min_length=1)
    #: The role of the responsible people: the summary and the notices about new bindings.
    ROLE_OWNER: str = Field(min_length=1)
    #: The role of the people the assistant calls when it has no answer.
    ROLE_ESCALATION: str = Field(min_length=1)
    #: The role of the people who may ask for aggregate figures over the data (totals, breakdowns).
    ROLE_ANALYTICS: str = Field(min_length=1)
    #: Where the provider lists that role: it may be a role of another application, e.g. the admin panel.
    ANALYTICS_ROLES_CLAIM: str = Field(min_length=1)

    ANTHROPIC_API_KEY: SecretStr
    #: How the service reaches the model's API: straight or through the proxy of the installation.
    #: The API refuses requests from some countries, whatever the key.
    MODEL_EGRESS: Literal["direct", "proxy"]
    MODEL_PROXY_URL: SecretStr | None = None
    MODEL: str = Field(min_length=1)
    MODEL_EFFORT: Literal["low", "medium", "high", "xhigh", "max"]
    #: How many tokens, input and output together, the model may spend on one request about a case.
    CASE_TOKEN_BUDGET: int = Field(gt=0)

    #: The directory of the installation's instructions: how to investigate, the sources, the personal data fields.
    INSTRUCTIONS_DIR: Path
    #: The Fernet key that encrypts the staff's offline tokens and the data of cases.
    ENCRYPTION_KEY: SecretStr
    #: After how many days without a turn a case is deleted, with its conversation and the client's data.
    CASE_RETENTION_DAYS: int = Field(gt=0)
    #: How often the cases that wait for a fix are checked again.
    FOLLOWUP_INTERVAL_MINUTES: int = Field(gt=0)

    DB_HOST: str = Field(min_length=1)
    DB_PORT: int = Field(gt=0)
    DB_USER: str = Field(min_length=1)
    DB_PASSWORD: SecretStr
    DB_NAME: str = Field(min_length=1)

    @field_validator("TELEGRAM_PROXY_URL", "MODEL_PROXY_URL", mode="before")
    @classmethod
    def empty_is_not_set(cls, value: object) -> object:
        # A compose file hands an unset variable over as an empty string.
        return None if value == "" else value

    @field_validator("TELEGRAM_GROUPS", mode="before")
    @classmethod
    def split_groups(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        groups = []
        for part in value.split(","):
            if not part.strip():
                continue
            try:
                groups.append(int(part.strip()))
            except ValueError as exc:
                raise ValueError(f"{part.strip()!r} is not the number of a chat") from exc
        return frozenset(groups)

    @field_validator("TELEGRAM_BOT_USERNAME")
    @classmethod
    def strip_at_sign(cls, value: str) -> str:
        return value.removeprefix("@")

    @field_validator("OIDC_ISSUER")
    @classmethod
    def strip_issuer_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def check_egress(self) -> "Settings":
        if self.TELEGRAM_EGRESS == "proxy" and self.TELEGRAM_PROXY_URL is None:
            raise ValueError("TELEGRAM_PROXY_URL: required while TELEGRAM_EGRESS is 'proxy'")
        if self.TELEGRAM_EGRESS == "direct" and self.TELEGRAM_PROXY_URL is not None:
            raise ValueError("TELEGRAM_PROXY_URL: set while TELEGRAM_EGRESS is 'direct'; one of the two is wrong")
        if self.MODEL_EGRESS == "proxy" and self.MODEL_PROXY_URL is None:
            raise ValueError("MODEL_PROXY_URL: required while MODEL_EGRESS is 'proxy'")
        if self.MODEL_EGRESS == "direct" and self.MODEL_PROXY_URL is not None:
            raise ValueError("MODEL_PROXY_URL: set while MODEL_EGRESS is 'direct'; one of the two is wrong")
        return self

    @property
    def database_url(self) -> URL:
        return URL.create(
            drivername="postgresql+asyncpg",
            username=self.DB_USER,
            password=self.DB_PASSWORD.get_secret_value(),
            host=self.DB_HOST,
            port=self.DB_PORT,
            database=self.DB_NAME,
        )

    @property
    def role_support(self) -> Role:
        return Role(self.OIDC_ROLES_CLAIM, self.ROLE_SUPPORT)

    @property
    def role_owner(self) -> Role:
        return Role(self.OIDC_ROLES_CLAIM, self.ROLE_OWNER)

    @property
    def role_escalation(self) -> Role:
        return Role(self.OIDC_ROLES_CLAIM, self.ROLE_ESCALATION)

    @property
    def role_analytics(self) -> Role:
        return Role(self.ANALYTICS_ROLES_CLAIM, self.ROLE_ANALYTICS)

    @property
    def telegram_proxy_url(self) -> str | None:
        return self.TELEGRAM_PROXY_URL.get_secret_value() if self.TELEGRAM_PROXY_URL is not None else None

    @property
    def model_proxy_url(self) -> str | None:
        return self.MODEL_PROXY_URL.get_secret_value() if self.MODEL_PROXY_URL is not None else None


def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        # Only the names of the settings and what is wrong with them: never the values.
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'settings'}: {error['msg']}" for error in exc.errors()
        )
        raise ConfigurationError(f"Invalid installation settings: {problems}") from exc
