from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "ADBAP Control Plane"
    app_version: str = "0.1.0"
    app_env: str = "development"
    database_url: str = "sqlite+aiosqlite:///./local.db"
    rewardlab_base_url: str = "http://127.0.0.1:8001"
    automation_allowed_domains: list[str] = ["localhost", "127.0.0.1", "rewardlab.local"]
    cors_allowed_origins: list[str] = [
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        "http://127.0.0.1:8001",
        "http://localhost:8001",
    ]
    ai_workflows_enabled: bool = False
    worker_max_concurrency: int = 2

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
