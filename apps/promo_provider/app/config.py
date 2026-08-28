from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "promo-provider"
    otlp_endpoint: str = "http://jaeger:4318"
    observability_ingest_url: str = "http://observability-service:8004/internal/spans"

    #: Baseline promo handling cost. Faults are Day 2; this is the healthy value.
    base_latency_ms: int = 25


settings = Settings()
