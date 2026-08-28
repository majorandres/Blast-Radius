from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "ordering-app"

    # --- required (v1.2 Day 1 config contract) ---
    database_url_app: str = "postgresql+asyncpg://blastradius_app:app@postgres:5432/blastradius"
    observability_ingest_url: str = "http://observability-service:8004/internal/spans"
    promo_provider_url: str = "http://promo-provider:8002"

    # --- optional ---
    otlp_endpoint: str = "http://jaeger:4318"
    traffic_enabled: bool = True
    traffic_base_rate_per_min: int = 150
    traffic_seed: int = 42
    traffic_max_concurrency: int = 40
    db_pool_size: int = 10
    db_pool_timeout: int = 5
    promo_client_timeout_ms: int = 2000


settings = Settings()
