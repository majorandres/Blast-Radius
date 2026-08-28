from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "scenario-controller"
    profile: str = "DEMO"

    database_url_scenario: str = (
        "postgresql+asyncpg://blastradius_scenario:scenario@postgres:5432/blastradius"
    )
    ordering_app_url: str = "http://ordering-app:8001"
    promo_provider_url: str = "http://promo-provider:8002"

    #: Read-only, and only ever the same public endpoint the frontend polls, so
    #: observability cannot distinguish it from a normal read (v1.2 §5.4).
    observability_url: str = "http://observability-service:8004"


settings = Settings()
