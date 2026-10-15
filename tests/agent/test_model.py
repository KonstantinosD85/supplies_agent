import asyncio

from scripted_model import ScriptedModel
from strands.models.anthropic import AnthropicModel

from supplies_agent.agent.model import build_model, close_model
from supplies_agent.agent.settings import AgentSettings


def test_anthropic_is_the_default_provider():
    model = build_model(AgentSettings(anthropic_api_key="test-key"))
    assert isinstance(model, AnthropicModel)
    assert model.get_config()["model_id"] == "claude-sonnet-5-5"


def test_close_model_closes_the_anthropic_client():
    model = build_model(AgentSettings(anthropic_api_key="test-key"))
    asyncio.run(close_model(model))
    assert model.client.is_closed()


def test_close_model_ignores_providers_without_an_async_client():
    asyncio.run(close_model(ScriptedModel()))
