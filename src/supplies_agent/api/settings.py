from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class FaultConfig(BaseModel):
    """Probabilities (0-1) of each injected fault, rolled independently per request."""

    rate_limit_rate: float = Field(0.0, ge=0, le=1, description="Chance of a 429 response.")
    server_error_rate: float = Field(
        0.0,
        ge=0,
        le=1,
        description="Chance of a 503. On writes it is returned after the write has committed.",
    )
    slow_rate: float = Field(0.0, ge=0, le=1, description="Chance of a delayed response.")
    slow_seconds: float = Field(5.0, ge=0, le=60, description="Delay applied to slow responses.")


class ApiSettings(BaseSettings):
    """Read from env vars prefixed API_, e.g. API_KEY, API_FAULTS__SLOW_RATE."""

    model_config = SettingsConfigDict(env_prefix="API_", env_nested_delimiter="__")

    key: str = "dev-mock-key"
    db_path: str = "data/mock.db"
    host: str = "127.0.0.1"
    port: int = 8001
    faults: FaultConfig = FaultConfig()
