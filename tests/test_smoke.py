from sqlalchemy import text


def test_database_is_reachable(engine):
    with engine.connect() as conn:
        version = conn.execute(text("select current_setting('server_version_num')::int")).scalar()
    assert version >= 160000  # Postgres 16+
