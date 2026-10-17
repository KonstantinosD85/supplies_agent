"""The one place that knows about model providers."""

from strands.models import Model

from supplies_agent.agent.settings import AgentSettings

DEFAULT_MODEL_IDS = {
    "anthropic": "claude-sonnet-5-5",
}


def build_model(settings: AgentSettings) -> Model:
    model_id = settings.model_id or DEFAULT_MODEL_IDS.get(settings.model_provider)

    match settings.model_provider:
        case "anthropic":
            from strands.models.anthropic import AnthropicModel

            client_args = {}
            if settings.anthropic_api_key:
                client_args["api_key"] = settings.anthropic_api_key.get_secret_value()
            return AnthropicModel(
                client_args=client_args, model_id=model_id, max_tokens=settings.max_tokens
            )
        case "bedrock":
            from strands.models import BedrockModel

            # Credentials and region come from the standard AWS environment.
            kwargs = {"model_id": model_id} if model_id else {}
            return BedrockModel(max_tokens=settings.max_tokens, **kwargs)


async def close_model(model: Model) -> None:
    """Release the provider's HTTP connections, on the event loop that opened them.

    Each request runs its invocation in its own event loop. The Anthropic client's async connection
    pool is bound to that loop; left open, it is closed later by garbage collection on a
    different loop and fails with "Event loop is closed". Bedrock uses boto3 (sync): nothing
    to close.
    """
    from strands.models.anthropic import AnthropicModel

    if isinstance(model, AnthropicModel):
        await model.client.close()
