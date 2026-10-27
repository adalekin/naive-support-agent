from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from internal.config import load_settings
from internal.entity import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def database_url() -> str:
    """The URL passed by the caller (the test suite), else the one from the installation settings.

    Migrations run over the synchronous driver.
    """
    configured = config.get_main_option("sqlalchemy.url")
    url = configured or load_settings().database_url.render_as_string(hide_password=False)
    return url.replace("+asyncpg", "+psycopg")


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(), target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"}
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
