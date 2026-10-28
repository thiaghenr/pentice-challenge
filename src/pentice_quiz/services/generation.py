import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from pentice_quiz.config import Settings
from pentice_quiz.db.models import (
    Building,
    LlmGenerationLog,
    Option,
    Question,
    Quiz,
    SourceSnapshot,
    utcnow,
)
from pentice_quiz.domain.schemas import (
    MAX_QUESTIONS_PER_SECTION,
    MIN_QUESTIONS,
    GeneratedOption,
    GeneratedQuestion,
    GenerationMode,
    LogStatus,
    QuizStatus,
    Section,
    Strategy,
    Topic,
)
from pentice_quiz.domain.scoring import question_weights
from pentice_quiz.domain.validation import validate_candidates, validate_llm_output
from pentice_quiz.errors import (
    GenerationUnavailableError,
    InvalidRequestError,
    SourceUnavailableError,
)
from pentice_quiz.llm.client import LLMClient, LLMError, LLMTimeoutError, Message
from pentice_quiz.llm.prompts import (
    PROMPT_VERSION,
    build_generation_messages,
    build_repair_messages,
)
from pentice_quiz.services import analytics
from pentice_quiz.services.fallback import (
    TEMPLATE_PROMPT_VERSION,
    build_template_questions,
    find_cached_quiz,
)
from pentice_quiz.services.ingestion import (
    FetchedSource,
    extract_title,
    fetch_markdown,
    split_sections,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GenerationLogEntry:
    provider: str
    model: str
    latency_ms: int
    status: LogStatus
    validation_errors: list[str]


@dataclass
class AgentResult:
    questions: list[GeneratedQuestion] = field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    logs: list[GenerationLogEntry] = field(default_factory=list)


class QuizAgent:
    def __init__(
        self,
        clients: Sequence[LLMClient],
        *,
        max_retries: int = 2,
        backoff_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._clients = list(clients)
        self._max_retries = max_retries
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep

    def generate(
        self,
        sections: Sequence[Section],
        *,
        num_questions: int,
        strategy: Strategy,
        topic_miss_rates: Mapping[Topic, float] | None = None,
    ) -> AgentResult:
        result = AgentResult()
        base_messages = build_generation_messages(
            sections,
            num_questions=num_questions,
            strategy=strategy,
            topic_miss_rates=topic_miss_rates,
        )
        max_per_section = MAX_QUESTIONS_PER_SECTION if strategy is Strategy.BY_SECTION else None
        for client in self._clients:
            questions = self._generate_with(
                client, base_messages, sections, num_questions, max_per_section, result.logs
            )
            if questions:
                result.questions = questions[:num_questions]
                result.provider, result.model = client.provider, client.model
                return result
            logger.warning(
                "llm client gave up", extra={"provider": client.provider, "model": client.model}
            )
        return result

    def _generate_with(
        self,
        client: LLMClient,
        base_messages: list[Message],
        sections: Sequence[Section],
        num_questions: int,
        max_per_section: int | None,
        logs: list[GenerationLogEntry],
    ) -> list[GeneratedQuestion] | None:
        messages = base_messages
        collected: list[GeneratedQuestion] = []
        for call_index in range(self._max_retries + 1):
            started = time.perf_counter()
            try:
                raw = client.complete_json(messages)
            except LLMError as exc:
                status = LogStatus.TIMEOUT if isinstance(exc, LLMTimeoutError) else LogStatus.ERROR
                self._log(logs, client, started, status, [str(exc)])
                if call_index < self._max_retries:
                    self._sleep(self._backoff_seconds * 2**call_index)
                continue

            outcome = validate_llm_output(
                raw, sections, existing=collected, max_per_section=max_per_section
            )
            collected.extend(outcome.valid)
            errors = list(outcome.errors)
            if len(collected) < MIN_QUESTIONS:
                errors.append(f"only {len(collected)} valid questions, need {MIN_QUESTIONS}")
            self._log(logs, client, started, LogStatus.INVALID if errors else LogStatus.OK, errors)
            if len(collected) >= MIN_QUESTIONS:
                return collected
            messages = build_repair_messages(
                base_messages,
                previous_output=raw,
                errors=errors,
                missing=num_questions - len(collected),
                accepted_questions=[question.text for question in collected],
            )
        return None

    @staticmethod
    def _log(
        logs: list[GenerationLogEntry],
        client: LLMClient,
        started: float,
        status: LogStatus,
        errors: list[str],
    ) -> None:
        latency_ms = round((time.perf_counter() - started) * 1000)
        logs.append(GenerationLogEntry(client.provider, client.model, latency_ms, status, errors))
        logger.info(
            "llm call",
            extra={
                "provider": client.provider,
                "model": client.model,
                "latency_ms": latency_ms,
                "status": status.value,
                "error_count": len(errors),
            },
        )


@dataclass(frozen=True, slots=True)
class QuizRequest:
    building_slug: str
    source_url: str | None = None
    strategy: Strategy = Strategy.BY_SECTION
    num_questions: int = 6
    force: bool = False


@dataclass(frozen=True, slots=True)
class QuizCreation:
    quiz: Quiz
    reused: bool


def create_quiz(
    session: Session,
    request: QuizRequest,
    *,
    settings: Settings,
    http_client: httpx.Client,
    agent: QuizAgent,
) -> QuizCreation:
    building = session.scalars(
        select(Building).where(Building.slug == request.building_slug)
    ).first()
    url = (
        request.source_url
        or (building.source_url if building else None)
        or settings.house_rules_url
    )
    if not url:
        raise InvalidRequestError(
            "no source_url given and HOUSE_RULES_URL is not configured", code="missing_source_url"
        )

    fetched = fetch_markdown(http_client, url, max_bytes=settings.source_max_bytes)
    sections = split_sections(fetched.content)
    if not sections:
        raise SourceUnavailableError("house rules have no readable sections", code="source_invalid")

    if building is None:
        building = Building(
            slug=request.building_slug,
            name=extract_title(fetched.content) or request.building_slug.replace("-", " ").title(),
            source_url=url,
        )
        session.add(building)
        session.flush()
    snapshot = _get_or_create_snapshot(session, building, fetched)

    if not request.force:
        existing = _find_quiz(session, building.id, fetched.content_hash, request.strategy)
        if existing is not None:
            session.commit()
            return QuizCreation(quiz=existing, reused=True)

    topic_miss_rates = (
        analytics.topic_miss_rates(session, building.id)
        if request.strategy is Strategy.BY_PAST_MISTAKES
        else None
    )
    result = agent.generate(
        sections,
        num_questions=request.num_questions,
        strategy=request.strategy,
        topic_miss_rates=topic_miss_rates,
    )

    quiz: Quiz | None = None
    if result.questions:
        quiz = _publish(
            session,
            building=building,
            snapshot=snapshot,
            questions=result.questions,
            strategy=request.strategy,
            mode=GenerationMode.LLM,
            provider=result.provider,
            model=result.model,
            prompt_version=PROMPT_VERSION,
        )
    else:
        quiz = _publish_fallback(session, building, snapshot, sections, request)

    for entry in result.logs:
        session.add(
            LlmGenerationLog(
                quiz_id=quiz.id if quiz else None,
                building_id=building.id,
                provider=entry.provider,
                model=entry.model,
                prompt_version=PROMPT_VERSION,
                latency_ms=entry.latency_ms,
                status=entry.status,
                validation_errors_json=entry.validation_errors,
            )
        )
    session.commit()

    if quiz is None:
        raise GenerationUnavailableError(
            "could not generate a valid quiz right now; please try again later"
        )
    logger.info(
        "quiz published",
        extra={"quiz_id": quiz.id, "building": building.slug, "mode": quiz.generation_mode.value},
    )
    return QuizCreation(quiz=quiz, reused=False)


def _get_or_create_snapshot(
    session: Session, building: Building, fetched: FetchedSource
) -> SourceSnapshot:
    snapshot = session.scalars(
        select(SourceSnapshot)
        .where(
            SourceSnapshot.building_id == building.id,
            SourceSnapshot.content_hash == fetched.content_hash,
        )
        .limit(1)
    ).first()
    if snapshot is None:
        snapshot = SourceSnapshot(
            building_id=building.id,
            url=fetched.url,
            content=fetched.content,
            content_hash=fetched.content_hash,
            etag=fetched.etag,
            last_modified=fetched.last_modified,
        )
        session.add(snapshot)
        session.flush()
    return snapshot


def _find_quiz(
    session: Session, building_id: str, content_hash: str, strategy: Strategy
) -> Quiz | None:
    statement = (
        select(Quiz)
        .join(SourceSnapshot, Quiz.source_snapshot_id == SourceSnapshot.id)
        .where(
            Quiz.building_id == building_id,
            Quiz.status == QuizStatus.PUBLISHED,
            Quiz.strategy == strategy,
            SourceSnapshot.content_hash == content_hash,
        )
        .order_by(Quiz.published_at.desc())
        .limit(1)
    )
    return session.scalars(statement).first()


def _publish_fallback(
    session: Session,
    building: Building,
    snapshot: SourceSnapshot,
    sections: Sequence[Section],
    request: QuizRequest,
) -> Quiz | None:
    cached = find_cached_quiz(session, building_id=building.id, content_hash=snapshot.content_hash)
    if cached is not None:
        logger.warning("llm unavailable, reusing cached quiz", extra={"cached_quiz_id": cached.id})
        return _publish(
            session,
            building=building,
            snapshot=snapshot,
            questions=[_to_generated(question) for question in cached.questions],
            strategy=request.strategy,
            mode=GenerationMode.FALLBACK_CACHE,
            provider=cached.llm_provider,
            model=cached.llm_model,
            prompt_version=cached.prompt_version,
        )

    candidates = build_template_questions(sections, request.num_questions)
    # Template output goes through the same validation as LLM output.
    outcome = validate_candidates(
        [candidate.model_dump(mode="json") for candidate in candidates],
        sections,
        max_per_section=MAX_QUESTIONS_PER_SECTION,
    )
    if len(outcome.valid) >= MIN_QUESTIONS:
        logger.warning("llm unavailable, publishing template quiz")
        return _publish(
            session,
            building=building,
            snapshot=snapshot,
            questions=outcome.valid,
            strategy=request.strategy,
            mode=GenerationMode.FALLBACK_TEMPLATE,
            provider=None,
            model=None,
            prompt_version=TEMPLATE_PROMPT_VERSION,
        )
    logger.error("all generation fallbacks failed", extra={"building": building.slug})
    return None


def _publish(
    session: Session,
    *,
    building: Building,
    snapshot: SourceSnapshot,
    questions: Sequence[GeneratedQuestion],
    strategy: Strategy,
    mode: GenerationMode,
    provider: str | None,
    model: str | None,
    prompt_version: str | None,
) -> Quiz:
    now = utcnow()
    quiz = Quiz(
        building_id=building.id,
        source_snapshot_id=snapshot.id,
        status=QuizStatus.PUBLISHED,
        strategy=strategy,
        llm_provider=provider,
        llm_model=model,
        prompt_version=prompt_version,
        generation_mode=mode,
        created_at=now,
        published_at=now,
    )
    weights = question_weights(len(questions))
    for position, (generated, weight) in enumerate(zip(questions, weights, strict=True), start=1):
        quiz.questions.append(
            Question(
                position=position,
                text=generated.text,
                topic=generated.topic,
                section=generated.section,
                source_quote=generated.source_quote,
                explanation=generated.explanation,
                multi_select=generated.multi_select,
                weight=weight,
                options=[
                    Option(position=index, text=option.text, is_correct=option.is_correct)
                    for index, option in enumerate(generated.options, start=1)
                ],
            )
        )
    session.add(quiz)
    session.flush()
    return quiz


def _to_generated(question: Question) -> GeneratedQuestion:
    return GeneratedQuestion(
        text=question.text,
        section=question.section,
        source_quote=question.source_quote,
        topic=question.topic,
        explanation=question.explanation,
        options=[
            GeneratedOption(text=option.text, is_correct=option.is_correct)
            for option in question.options
        ],
    )
