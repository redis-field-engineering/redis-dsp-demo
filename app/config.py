from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "redis-dsp-demo"
    redis_url: str = "redis://localhost:6379/0"
    dataset_dir: Path = Path("data/generated/synthetic")
    top_k: int = 5
    max_candidates: int = 50
    strong_signal_count: int = 2
    cache_campaigns_in_memory: bool = True
    auto_bootstrap_data: bool = False
    synthetic_users: int = Field(default=4000, ge=100)
    synthetic_campaigns: int = Field(default=2500, ge=100)
    synthetic_interactions: int = Field(default=120000, ge=1000)
    synthetic_feature_count: int = Field(default=12, ge=5)
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"
    otel_service_name: str = "redis-dsp-demo"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
