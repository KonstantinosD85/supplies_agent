import httpx2
import pytest

from supplies_agent.api import db
from supplies_agent.api.app import create_app
from supplies_agent.api.settings import ApiSettings
from supplies_agent.mcp_server.client import SuppliesApi
from supplies_agent.mcp_server.settings import McpSettings

API_KEY = "test-key"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def mcp_settings() -> McpSettings:
    # Short timeouts so retry tests run fast.
    return McpSettings(
        api_url="http://supplies-api",
        api_key=API_KEY,
        max_attempts=4,
        attempt_timeout=0.2,
        total_timeout=2.0,
        backoff_base=0.01,
    )


@pytest.fixture
def supplies_app(tmp_path):
    """The real supplies API (mock) app, served in-process. Shared by the MCP and agent tests."""
    settings = ApiSettings(key=API_KEY, db_path=str(tmp_path / "api.db"))
    db.init_db(settings.db_path)  # ASGITransport does not run the app's lifespan.
    return create_app(settings)


@pytest.fixture
def api(mcp_settings, supplies_app) -> SuppliesApi:
    return SuppliesApi(mcp_settings, transport=httpx2.ASGITransport(app=supplies_app))
