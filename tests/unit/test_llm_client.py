import json
from collections.abc import Callable

import httpx2
import openai
import pytest
from pydantic import SecretStr

from pentice_quiz.config import Settings
from pentice_quiz.llm.client import (
    LLMError,
    LLMTimeoutError,
    OpenAICompatibleClient,
    build_llm_clients,
)

BASE_URL = "https://llm.example.com/v1"
MESSAGES = [{"role": "user", "content": "hi"}]
Handler = Callable[[httpx2.Request], httpx2.Response]


def _client(
    handler: Handler, requests: list[httpx2.Request] | None = None
) -> OpenAICompatibleClient:
    def recording(request: httpx2.Request) -> httpx2.Response:
        if requests is not None:
            requests.append(request)
        return handler(request)

    sdk = openai.OpenAI(
        base_url=BASE_URL,
        api_key="k",
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(recording)),
    )
    return OpenAICompatibleClient(
        provider="openai",
        model="m",
        base_url=BASE_URL,
        api_key="k",
        timeout_seconds=1,
        sdk_client=sdk,
    )


def _completion(content: str | None) -> Handler:
    body = {
        "id": "c1",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
    }
    return lambda _request: httpx2.Response(200, json=body)


def test_requests_json_mode_and_returns_content() -> None:
    requests: list[httpx2.Request] = []
    client = _client(_completion('{"questions": []}'), requests)
    assert client.complete_json(MESSAGES) == '{"questions": []}'
    sent = json.loads(requests[0].content)
    assert sent["response_format"] == {"type": "json_object"}
    assert sent["messages"] == MESSAGES


def test_timeout_is_mapped() -> None:
    def slow(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    with pytest.raises(LLMTimeoutError):
        _client(slow).complete_json(MESSAGES)


def test_http_error_is_mapped_without_sdk_retries() -> None:
    requests: list[httpx2.Request] = []
    client = _client(lambda _r: httpx2.Response(500, json={"error": "boom"}), requests)
    with pytest.raises(LLMError, match="openai"):
        client.complete_json(MESSAGES)
    assert len(requests) == 1


def test_empty_completion_is_an_error() -> None:
    with pytest.raises(LLMError, match="empty"):
        _client(_completion(None)).complete_json(MESSAGES)


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_build_clients_primary_and_secondary() -> None:
    clients = build_llm_clients(
        _settings(
            openai_api_key=SecretStr("g"),
            openrouter_api_key=SecretStr("o"),
            llm_fallback_provider="openrouter",
            llm_fallback_model="meta-llama/llama-3.3-70b-instruct",
        )
    )
    assert [(c.provider, c.model) for c in clients] == [
        ("openai", "gpt-4.1-mini"),
        ("openrouter", "meta-llama/llama-3.3-70b-instruct"),
    ]


def test_build_clients_skips_providers_without_key() -> None:
    assert build_llm_clients(_settings(openai_api_key=None)) == []


def test_ollama_needs_no_key() -> None:
    clients = build_llm_clients(_settings(llm_provider="ollama", llm_model="llama3.1"))
    assert [c.provider for c in clients] == ["ollama"]


def test_blank_api_key_counts_as_missing() -> None:
    assert build_llm_clients(_settings(openai_api_key=SecretStr("  "))) == []


def test_empty_env_var_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    assert Settings(_env_file=None).openai_api_key is None
