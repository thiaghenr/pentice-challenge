import copy
import json
import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from pentice_quiz.config import Settings
from pentice_quiz.db.models import Base
from pentice_quiz.db.session import create_db_engine, create_session_factory
from pentice_quiz.domain.schemas import Section
from pentice_quiz.llm.client import LLMError, Message
from pentice_quiz.main import create_app
from pentice_quiz.services.ingestion import split_sections

FIXTURES = Path(__file__).parent / "fixtures"
HARBOR_URL = "https://rules.example.com/house-rules-harbor-lofts.md"
PINE_URL = "https://rules.example.com/house-rules-pine-court.md"
API_KEY = "test-ops-key"
# Tests need a Postgres: `make db` starts one (docker compose) with this database.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://pentice:pentice@localhost:5432/pentice_test"
)


def load_rules(name: str) -> str:
    return (FIXTURES / "house_rules" / f"house-rules-{name}.md").read_text(encoding="utf-8")


def load_llm_output(name: str) -> str:
    return (FIXTURES / "llm_outputs" / name).read_text(encoding="utf-8")


def llm_questions(name: str) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = json.loads(load_llm_output(name))["questions"]
    return copy.deepcopy(questions)


def as_llm_reply(questions: Sequence[dict[str, Any]]) -> str:
    return json.dumps({"questions": list(questions)})


@dataclass
class FakeLLMClient:
    replies: list[str | Exception]
    provider: str = "fake"
    model: str = "fake-model"
    calls: list[list[Message]] = field(default_factory=list)

    def complete_json(self, messages: Sequence[Message]) -> str:
        self.calls.append(list(messages))
        if not self.replies:
            raise LLMError("fake LLM has no more scripted replies")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def harbor_rules() -> str:
    return load_rules("harbor-lofts")


@pytest.fixture
def pine_rules() -> str:
    return load_rules("pine-court")


@pytest.fixture
def harbor_sections(harbor_rules: str) -> list[Section]:
    return split_sections(harbor_rules)


@pytest.fixture
def pine_sections(pine_rules: str) -> list[Section]:
    return split_sections(pine_rules)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url=TEST_DATABASE_URL,
        ops_api_key=SecretStr(API_KEY),
        house_rules_url=HARBOR_URL,
        pass_threshold_pct=75.0,
        missed_min_attempts=1,
        log_level="WARNING",
    )


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    engine = create_db_engine(TEST_DATABASE_URL)
    try:
        with engine.connect():
            pass
    except OperationalError as exc:
        pytest.fail(
            f"Postgres is not reachable at {TEST_DATABASE_URL} ({type(exc).__name__}). "
            "Start it with `make db` or set TEST_DATABASE_URL.",
            pytrace=False,
        )
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(db_engine: Engine) -> Iterator[sessionmaker[Session]]:
    yield create_session_factory(db_engine)
    # Each test starts from empty tables; the schema is created once per run.
    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    with db_engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as db_session:
        yield db_session


@pytest.fixture
def source_documents(harbor_rules: str, pine_rules: str) -> dict[str, str]:
    return {HARBOR_URL: harbor_rules, PINE_URL: pine_rules}


@pytest.fixture
def rules_http_client(source_documents: dict[str, str]) -> Iterator[httpx.Client]:
    def handler(request: httpx.Request) -> httpx.Response:
        content = source_documents.get(str(request.url))
        if content is None:
            return httpx.Response(404, text="not found")
        return httpx.Response(
            200, text=content, headers={"content-type": "text/plain; charset=utf-8"}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        yield client


@pytest.fixture
def fake_llm() -> FakeLLMClient:
    return FakeLLMClient(replies=[])


@pytest.fixture
def client(
    settings: Settings,
    session_factory: sessionmaker[Session],
    rules_http_client: httpx.Client,
    fake_llm: FakeLLMClient,
) -> Iterator[TestClient]:
    app = create_app(
        settings,
        llm_clients=[fake_llm],
        http_client=rules_http_client,
        session_factory=session_factory,
        sleep=lambda _seconds: None,
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY}
