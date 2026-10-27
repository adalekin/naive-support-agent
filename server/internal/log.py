"""The service log: whatever is written to it passes through ``Redactor`` first.

The log is read by people who were given none of the service's secrets, so a secret
that slips into a message, an exception or a traceback is cut out before it is written.
"""

import re
import sys
from collections.abc import Iterable
from typing import TextIO

from loguru import logger

REDACTED = "[redacted]"

#: Shapes of secrets the service handles without knowing them in advance.
SECRET_SHAPES = (
    # A JSON Web Token: every access, refresh and offline token the provider issues.
    re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"),
    # A Telegram bot token, also inside the address of every Bot API call.
    re.compile(r"(?<!\d)\d{5,}:[A-Za-z0-9_-]{30,}"),
    # A password inside an address: scheme://user:password@host.
    re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),
)


class Redactor:
    def __init__(self, secrets: Iterable[str]) -> None:
        # Longest first, so that a secret containing another is cut out whole.
        self._secrets = sorted({secret for secret in secrets if secret}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        for shape in SECRET_SHAPES:
            text = shape.sub(REDACTED, text)
        return text


def configure_logging(secrets: Iterable[str], stream: TextIO = sys.stderr) -> None:
    redact = Redactor(secrets)

    def sink(message: str) -> None:
        stream.write(redact(str(message)))
        stream.flush()

    logger.remove()
    # No variable values in tracebacks: they would carry tokens and personal data.
    logger.add(sink, level="INFO", backtrace=False, diagnose=False)
