import logging
from collections.abc import Sequence
from typing import Protocol, cast

import openai
from openai.types.chat import ChatCompletionMessageParam

from pentice_quiz.config import LLMProvider, Settings

logger = logging.getLogger(__name__)

Message = dict[str, str]

PROVIDER_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}


class LLMError(Exception):
    pass


class LLMTimeoutError(LLMError):
    pass


class LLMClient(Protocol):
    provider: str
    model: str

    def complete_json(self, messages: Sequence[Message]) -> str: ...


class OpenAICompatibleClient:
    def __init__(
        self,
        *,
        provider: str,
        model: str,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
        sdk_client: openai.OpenAI | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self._client = sdk_client or openai.OpenAI(
            base_url=base_url, api_key=api_key, timeout=timeout_seconds, max_retries=0
        )

    def complete_json(self, messages: Sequence[Message]) -> str:
        try:
            completion = self._client.chat.completions.create(
                model=self.model,
                messages=cast(list[ChatCompletionMessageParam], list(messages)),
                response_format={"type": "json_object"},
                temperature=0.2,
            )
        except openai.APITimeoutError as exc:
            raise LLMTimeoutError(f"{self.provider} timed out") from exc
        except openai.OpenAIError as exc:
            raise LLMError(f"{self.provider}: {type(exc).__name__}: {exc}") from exc

        content = completion.choices[0].message.content if completion.choices else None
        if not content:
            raise LLMError(f"{self.provider} returned an empty completion")
        return content


def build_llm_clients(settings: Settings) -> list[LLMClient]:
    clients: list[LLMClient] = []
    primary = _make_client(settings.llm_provider, settings.llm_model, settings)
    if primary is not None:
        clients.append(primary)
    if settings.llm_fallback_model:
        provider = settings.llm_fallback_provider or settings.llm_provider
        secondary = _make_client(provider, settings.llm_fallback_model, settings)
        if secondary is not None:
            clients.append(secondary)
    if not clients:
        logger.warning("no LLM provider configured; quizzes will use the fallbacks only")
    return clients


def _make_client(provider: LLMProvider, model: str, settings: Settings) -> LLMClient | None:
    if provider == "ollama":
        base_url, api_key = settings.ollama_base_url, "ollama"
    else:
        secret = settings.openai_api_key if provider == "openai" else settings.openrouter_api_key
        if secret is None or not secret.get_secret_value().strip():
            logger.warning("LLM provider skipped: API key missing", extra={"provider": provider})
            return None
        base_url, api_key = PROVIDER_BASE_URLS[provider], secret.get_secret_value()
    return OpenAICompatibleClient(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=api_key,
        timeout_seconds=settings.llm_timeout_seconds,
    )
