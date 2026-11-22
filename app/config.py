from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings read from environment variables or .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://returns:returns@localhost:5433/returns"
    test_database_url: str = "postgresql+psycopg://returns:returns@localhost:5433/returns_test"


settings = Settings()
