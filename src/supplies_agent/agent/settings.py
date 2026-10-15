from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    """Read from env vars prefixed AGENT_ (and a local .env file), e.g. AGENT_MODEL_ID."""

    model_config = SettingsConfigDict(env_prefix="AGENT_", env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8080  # AgentCore Runtime HTTP contract

    # Model provider. Switch with AGENT_MODEL_PROVIDER / AGENT_MODEL_ID.
    model_provider: Literal["anthropic", "bedrock"] = "anthropic"
    model_id: str | None = Field(None, description="Defaults to the provider's default below.")
    max_tokens: int = 2048
    anthropic_api_key: SecretStr | None = Field(None, validation_alias="ANTHROPIC_API_KEY")

    # Short-term memory: conversation history per session (SQLite, local only).
    db_path: str = "data/agent.db"
    session_retention_days: int = Field(30, ge=1, description="Days of inactivity before purge.")

    # MCP server with the supplies tools.
    mcp_url: str = "http://127.0.0.1:8002/mcp"
