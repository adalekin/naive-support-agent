import json
from typing import Any, ClassVar

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Dialect, LargeBinary
from sqlalchemy.types import TypeDecorator


class TokenCipher:
    """Encrypts what would let someone act as a member of staff: offline tokens and device codes."""

    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode())

    def seal(self, secret: str) -> bytes:
        return self._fernet.encrypt(secret.encode())

    def open(self, sealed: bytes) -> str:
        """Raises ``InvalidToken`` when the key of the installation changed since the value was sealed."""
        return self._fernet.decrypt(sealed).decode()


__all__ = ["EncryptedJson", "InvalidToken", "TokenCipher"]


class EncryptedJson(TypeDecorator[Any]):
    """A JSON column stored encrypted: the data of a case holds the client's personal data as it is.

    The key is the installation's, set once for the process by ``Database``.
    """

    impl = LargeBinary
    cache_ok = True
    cipher: ClassVar[TokenCipher | None] = None

    def process_bind_param(self, value: Any, dialect: Dialect) -> bytes | None:
        if value is None:
            return None
        return self._cipher().seal(json.dumps(value, ensure_ascii=False))

    def process_result_value(self, value: bytes | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        return json.loads(self._cipher().open(value))

    @classmethod
    def _cipher(cls) -> TokenCipher:
        if cls.cipher is None:
            raise RuntimeError("The data of cases cannot be read or written before the key is set")
        return cls.cipher
