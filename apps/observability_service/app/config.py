from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "observability-service"
    profile: str = "DEMO"
    database_url_detector: str = (
        "postgresql+asyncpg://blastradius_detector:detector@postgres:5432/blastradius"
    )


settings = Settings()
