from sqlalchemy import create_engine
from sqlalchemy.engine import Engine


def make_engine(url: str) -> Engine:
    # pool_pre_ping drops dead connections instead of failing the next query.
    return create_engine(url, pool_pre_ping=True)
