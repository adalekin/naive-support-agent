import asyncio
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))

from internal.config import ConfigurationError, InstructionSet, load_settings
from internal.config.instructions import McpSourceSpec
from internal.log import configure_logging


def main() -> None:
    try:
        settings = load_settings()
        instructions = InstructionSet.load(settings.INSTRUCTIONS_DIR)
    except ConfigurationError as exc:
        # Refuse to start and name the setting; a traceback would bury it.
        sys.exit(str(exc))

    own = [
        settings.TELEGRAM_BOT_TOKEN,
        settings.OIDC_CLIENT_SECRET,
        settings.ANTHROPIC_API_KEY,
        settings.ENCRYPTION_KEY,
        settings.DB_PASSWORD,
        *([settings.TELEGRAM_PROXY_URL] if settings.TELEGRAM_PROXY_URL else []),
    ]
    # What the sources are given: passwords of their databases, tokens of their services.
    handed = [
        os.environ[name]
        for spec in instructions.sources
        for name in (spec.env if isinstance(spec, McpSourceSpec) else [spec.token_env] if spec.token_env else [])
        if name in os.environ
    ]
    configure_logging([secret.get_secret_value() for secret in own] + handed)
    from internal.app import Assistant

    asyncio.run(Assistant(settings, instructions).run())


if __name__ == "__main__":
    main()
