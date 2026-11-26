from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings read from environment variables or .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://returns:returns@localhost:5433/returns"
    test_database_url: str = "postgresql+psycopg://returns:returns@localhost:5433/returns_test"

    # The shop the app serves, and whether POST /reset may wipe and reload it.
    fixture: str = "two_shirts_v1"
    allow_reset: bool = False

    # Local model via Ollama. The digest pins the exact build used for results.
    ollama_url: str = "http://localhost:11434"
    llm_model: str = "qwen3.5:4b"
    llm_digest: str = "7f3251fa6878"
    llm_temperature: float = 0.2   # not 0: at 0 this model repeats a malformed tool call
    llm_seed: int = 7
    llm_num_ctx: int = 4096
    llm_timeout_s: int = 300


settings = Settings()
