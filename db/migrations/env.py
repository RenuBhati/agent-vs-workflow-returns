"""Alembic entry point.

Uses DATABASE_URL from app.config unless the caller has already set
sqlalchemy.url (the test suite does this to point at returns_test).
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from app.config import settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

url = config.get_main_option("sqlalchemy.url") or settings.database_url


def run_migrations_offline() -> None:
    context.configure(url=url, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
