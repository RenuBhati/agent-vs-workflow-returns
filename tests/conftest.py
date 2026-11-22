import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.config import settings
from app.db import make_engine

TABLES = (
    "review_cases, actions, tool_calls, messages, proposals, sessions, "
    "order_units, orders, skus, styles, shoppers"
)


@pytest.fixture(scope="session")
def engine():
    """Test database rebuilt from the migrations once per test run."""
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", settings.test_database_url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    eng = make_engine(settings.test_database_url)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    """Empty tables before each test, so tests never see each other's rows."""
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    return engine
