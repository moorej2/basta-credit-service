from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CREDIT_", env_file=".env", extra="ignore")

    mongo_uri: str = "mongodb://localhost:27017/?replicaSet=rs0&directConnection=true"
    mongo_database: str = "credits"
    broker_url: str = "amqp://credits:credits@localhost:5672//"
    dispatch_interval_seconds: float = Field(default=2, ge=0.1)
    dispatch_lease_seconds: int = Field(default=30, ge=1)
    dispatch_backoff_max_seconds: int = Field(default=300, ge=1)
    task_retry_base_seconds: int = Field(default=2, ge=1)
    task_retry_max_seconds: int = Field(default=60, ge=1)
    task_max_failures: int = Field(default=5, ge=1)
    mongo_timeout_ms: int = Field(default=3000, ge=100)
    mongo_socket_timeout_ms: int = Field(default=10000, ge=100)


@lru_cache
def get_settings() -> Settings:
    return Settings()
