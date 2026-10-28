import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from pentice_quiz.api import attempts, ops, quizzes
from pentice_quiz.api.deps import SessionDep
from pentice_quiz.api.errors import error_response, register_error_handlers
from pentice_quiz.config import Settings, get_settings
from pentice_quiz.db.session import create_db_engine, create_session_factory
from pentice_quiz.llm.client import LLMClient, build_llm_clients
from pentice_quiz.observability import configure_logging, request_id_var
from pentice_quiz.services.generation import QuizAgent

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"


def create_app(
    settings: Settings | None = None,
    *,
    llm_clients: Sequence[LLMClient] | None = None,
    http_client: httpx.Client | None = None,
    session_factory: sessionmaker[Session] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    if session_factory is None:
        session_factory = create_session_factory(create_db_engine(settings.database_url))
    owns_http_client = http_client is None
    source_client = http_client or httpx.Client(
        timeout=settings.source_timeout_seconds, follow_redirects=True
    )
    clients = list(llm_clients) if llm_clients is not None else build_llm_clients(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        if owns_http_client:
            source_client.close()

    app = FastAPI(
        title="Pentice Pre-Arrival Quiz",
        version="0.1.0",
        description="Quizzes generated from building house rules, with grounded questions.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.http_client = source_client
    app.state.quiz_agent = QuizAgent(clients, max_retries=settings.llm_max_retries, sleep=sleep)

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            logger.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                },
            )
            return response
        finally:
            request_id_var.reset(token)

    register_error_handlers(app)
    app.include_router(quizzes.router, prefix=API_PREFIX)
    app.include_router(attempts.router, prefix=API_PREFIX)
    app.include_router(ops.router, prefix=API_PREFIX)

    @app.get("/healthz", tags=["operations"], response_model=None)
    def healthz(session: SessionDep) -> dict[str, str] | Response:
        try:
            session.execute(text("SELECT 1"))
        except Exception:
            logger.exception("database health check failed")
            return error_response(503, "database_unavailable", "database is not reachable")
        return {"status": "ok", "database": "ok"}

    return app
