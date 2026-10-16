from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class McpSettings(BaseSettings):
    """Read from env vars prefixed MCP_, e.g. MCP_API_URL, MCP_API_KEY."""

    model_config = SettingsConfigDict(env_prefix="MCP_")

    host: str = "127.0.0.1"
    port: int = 8002
    # DNS-rebinding protection: only requests with one of these Host headers are served, whatever
    # the bind address. In compose, add the service name, e.g. '["mcp-server:*", "localhost:*"]'.
    allowed_hosts: list[str] = ["127.0.0.1:*", "localhost:*", "[::1]:*"]

    # How to reach the supplies API (the mock).
    api_url: str = "http://127.0.0.1:8001"
    api_key: str = "dev-mock-key"

    # Retry policy for calls to the supplies API.
    max_attempts: int = Field(4, ge=1)
    attempt_timeout: float = Field(
        3.0, gt=0, description="Seconds before one attempt is abandoned."
    )
    total_timeout: float = Field(15.0, gt=0, description="Seconds before the whole call gives up.")
    backoff_base: float = Field(0.5, ge=0, description="First retry delay; doubles each attempt.")
