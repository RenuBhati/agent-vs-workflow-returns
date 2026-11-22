import pytest

from app.config import settings
from app.db import make_engine


@pytest.fixture(scope="session")
def engine():
    eng = make_engine(settings.test_database_url)
    yield eng
    eng.dispose()
