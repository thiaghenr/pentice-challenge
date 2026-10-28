import secrets
from collections.abc import Iterator
from typing import Annotated

import httpx
from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from pentice_quiz.config import Settings
from pentice_quiz.errors import UnauthorizedError
from pentice_quiz.services.generation import QuizAgent


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


def get_app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_http_client(request: Request) -> httpx.Client:
    client: httpx.Client = request.app.state.http_client
    return client


def get_quiz_agent(request: Request) -> QuizAgent:
    agent: QuizAgent = request.app.state.quiz_agent
    return agent


SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
HttpClientDep = Annotated[httpx.Client, Depends(get_http_client)]
QuizAgentDep = Annotated[QuizAgent, Depends(get_quiz_agent)]


def require_api_key(
    settings: SettingsDep, x_api_key: Annotated[str | None, Header()] = None
) -> None:
    expected = settings.ops_api_key
    if expected is None:
        raise UnauthorizedError("OPS_API_KEY is not configured on the server")
    if x_api_key is None or not secrets.compare_digest(
        x_api_key.encode(), expected.get_secret_value().encode()
    ):
        raise UnauthorizedError("missing or invalid X-API-Key header")
