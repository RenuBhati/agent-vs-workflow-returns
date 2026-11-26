from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings read from environment variables or .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://returns:returns@localhost:5433/returns"
    test_database_url: str = "postgresql+psycopg://returns:returns@localhost:5433/returns_test"

    # The shop the app serves, and whether POST /reset may wipe and reload it.
    fixture: str = "two_shirts_v1"
    allow_reset: bool = False


settings = Settings()
