from functools import lru_cache
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    db_host: str = 'postgres'
    db_name: str = 'trading'
    db_user: str = 'trading'
    db_password_file: str = '/run/secrets/postgres_password'
    dhan_client_id_file: str = '/run/secrets/dhan_client_id'
    dhan_access_token_file: str = '/run/secrets/dhan_access_token'
    api_key_file: str = '/run/secrets/api_key'
    redis_url: str = 'redis://redis:6379/0'
    dhan_min_spacing_ms: int = Field(default=1100, ge=250)
    dhan_daily_guard: int = Field(default=90000, ge=1, le=100000)
    max_fetch_attempts: int = Field(default=4, ge=1, le=8)
    backoff_base_seconds: int = Field(default=5, ge=1)
    backoff_cap_seconds: int = Field(default=300, ge=5)
    collect_nse_bse: bool = False
    parallel_budget_required: bool = False
    history_queue_bound: int = Field(default=500, ge=1, le=2000)
    history_database_soft_gib: int = Field(default=20, ge=1)


def read_secret(path: str) -> str:
    # Read on every use, so a replaced token is picked up without cached credentials.
    return Path(path).read_text().strip()


@lru_cache
def settings() -> Settings:
    return Settings()
