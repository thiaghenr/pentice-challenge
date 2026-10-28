from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from pentice_quiz.db.models import Attempt, Option, Question, Quiz
from pentice_quiz.domain.schemas import (
    MAX_QUESTIONS,
    MIN_QUESTIONS,
    OPTIONS_PER_QUESTION,
    AttemptStatus,
    GenerationMode,
    QuizStatus,
    Strategy,
    Topic,
)
from pentice_quiz.domain.scoring import MAX_QUESTION_SCORE
from pentice_quiz.services.analytics import (
    QuestionMissStat,
    TopicMissStat,
    missed_topics_for_attempt,
)


def _display(value: float) -> float:
    return round(value, 2)


# ---------- requests ----------


class CreateQuizIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    building_slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=100)
    source_url: HttpUrl | None = None
    strategy: Strategy = Strategy.BY_SECTION
    num_questions: int = Field(default=6, ge=MIN_QUESTIONS, le=MAX_QUESTIONS)
    force: bool = Field(default=False, description="Regenerate even if the rules did not change")


class StartAttemptIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    guest_ref: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")


class SaveAnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    option_ids: list[str] = Field(max_length=OPTIONS_PER_QUESTION)


# ---------- quizzes ----------


class OptionOut(BaseModel):
    id: str
    position: int
    text: str


class QuestionOut(BaseModel):
    id: str
    position: int
    text: str
    topic: Topic
    section: str
    multi_select: bool
    options: list[OptionOut]


class QuizOut(BaseModel):
    id: str
    building_slug: str
    status: QuizStatus
    strategy: Strategy
    generation_mode: GenerationMode
    is_fallback: bool = Field(description="True when the quiz did not come from a fresh LLM call")
    prompt_version: str | None
    llm_provider: str | None
    llm_model: str | None
    published_at: datetime | None
    questions: list[QuestionOut]


class QuizCreatedOut(QuizOut):
    reused: bool = Field(description="True when the rules were unchanged and the quiz was reused")


class OptionAdminOut(BaseModel):
    id: str
    position: int
    text: str
    is_correct: bool


class QuestionAdminOut(BaseModel):
    id: str
    position: int
    text: str
    topic: Topic
    section: str
    multi_select: bool
    source_quote: str
    explanation: str
    weight: float
    options: list[OptionAdminOut]


class SourceSnapshotOut(BaseModel):
    url: str
    content_hash: str
    fetched_at: datetime


class QuizAdminOut(BaseModel):
    id: str
    building_slug: str
    status: QuizStatus
    strategy: Strategy
    generation_mode: GenerationMode
    prompt_version: str | None
    llm_provider: str | None
    llm_model: str | None
    published_at: datetime | None
    source: SourceSnapshotOut
    questions: list[QuestionAdminOut]


def _question_out(question: Question, position: int | None = None) -> QuestionOut:
    return QuestionOut(
        id=question.id,
        position=position or question.position,
        text=question.text,
        topic=question.topic,
        section=question.section,
        multi_select=question.multi_select,
        options=[_option_out(option) for option in question.options],
    )


def _option_out(option: Option) -> OptionOut:
    return OptionOut(id=option.id, position=option.position, text=option.text)


def _option_admin_out(option: Option) -> OptionAdminOut:
    return OptionAdminOut(
        id=option.id, position=option.position, text=option.text, is_correct=option.is_correct
    )


def quiz_out(quiz: Quiz) -> QuizOut:
    return QuizOut(
        id=quiz.id,
        building_slug=quiz.building.slug,
        status=quiz.status,
        strategy=quiz.strategy,
        generation_mode=quiz.generation_mode,
        is_fallback=quiz.generation_mode is not GenerationMode.LLM,
        prompt_version=quiz.prompt_version,
        llm_provider=quiz.llm_provider,
        llm_model=quiz.llm_model,
        published_at=quiz.published_at,
        questions=[_question_out(question) for question in quiz.questions],
    )


def quiz_admin_out(quiz: Quiz) -> QuizAdminOut:
    snapshot = quiz.source_snapshot
    return QuizAdminOut(
        id=quiz.id,
        building_slug=quiz.building.slug,
        status=quiz.status,
        strategy=quiz.strategy,
        generation_mode=quiz.generation_mode,
        prompt_version=quiz.prompt_version,
        llm_provider=quiz.llm_provider,
        llm_model=quiz.llm_model,
        published_at=quiz.published_at,
        source=SourceSnapshotOut(
            url=snapshot.url, content_hash=snapshot.content_hash, fetched_at=snapshot.fetched_at
        ),
        questions=[
            QuestionAdminOut(
                id=question.id,
                position=question.position,
                text=question.text,
                topic=question.topic,
                section=question.section,
                multi_select=question.multi_select,
                source_quote=question.source_quote,
                explanation=question.explanation,
                weight=question.weight,
                options=[_option_admin_out(option) for option in question.options],
            )
            for question in quiz.questions
        ],
    )


# ---------- attempts ----------


class AttemptOut(BaseModel):
    id: str
    quiz_id: str
    guest_ref: str
    attempt_number: int
    previous_attempt_id: str | None
    status: AttemptStatus
    started_at: datetime
    submitted_at: datetime | None
    questions: list[QuestionOut] = Field(description="In the order frozen for this attempt")
    answers: dict[str, list[str]] = Field(description="question_id -> selected option ids")


class AnswerOut(BaseModel):
    question_id: str
    option_ids: list[str]
    answered_at: datetime


class QuestionResultOut(BaseModel):
    question_id: str
    position: int
    text: str
    topic: Topic
    multi_select: bool
    options: list[OptionAdminOut]
    selected_option_ids: list[str]
    correct_option_ids: list[str]
    score: float
    max_score: float
    weight: float
    is_correct: bool
    explanation: str
    source_quote: str


class AttemptResultOut(BaseModel):
    attempt_id: str
    quiz_id: str
    guest_ref: str
    attempt_number: int
    submitted_at: datetime
    final_score: float = Field(description="Weighted score on the 0-4 scale")
    final_pct: float
    passed: bool
    questions: list[QuestionResultOut]


def _questions_in_order(attempt: Attempt) -> list[Question]:
    by_id = {question.id: question for question in attempt.quiz.questions}
    return [by_id[question_id] for question_id in attempt.question_order_json]


def attempt_out(attempt: Attempt) -> AttemptOut:
    return AttemptOut(
        id=attempt.id,
        quiz_id=attempt.quiz_id,
        guest_ref=attempt.guest_ref,
        attempt_number=attempt.attempt_number,
        previous_attempt_id=attempt.previous_attempt_id,
        status=attempt.status,
        started_at=attempt.started_at,
        submitted_at=attempt.submitted_at,
        questions=[
            _question_out(question, position)
            for position, question in enumerate(_questions_in_order(attempt), start=1)
        ],
        answers={
            answer.question_id: list(answer.selected_option_ids_json) for answer in attempt.answers
        },
    )


def attempt_result_out(attempt: Attempt) -> AttemptResultOut:
    if (
        attempt.submitted_at is None
        or attempt.final_score is None
        or attempt.final_pct is None
        or attempt.passed is None
    ):
        raise ValueError("attempt is not submitted")
    answers = {answer.question_id: answer for answer in attempt.answers}
    weights = {question.id: question.weight for question in attempt.quiz.questions}
    results = []
    for position, question in enumerate(_questions_in_order(attempt), start=1):
        answer = answers[question.id]
        results.append(
            QuestionResultOut(
                question_id=question.id,
                position=position,
                text=question.text,
                topic=question.topic,
                multi_select=question.multi_select,
                options=[_option_admin_out(option) for option in question.options],
                selected_option_ids=list(answer.selected_option_ids_json),
                correct_option_ids=[option.id for option in question.options if option.is_correct],
                score=_display(answer.score or 0.0),
                max_score=MAX_QUESTION_SCORE,
                weight=round(weights[question.id], 4),
                is_correct=bool(answer.is_correct),
                explanation=question.explanation,
                source_quote=question.source_quote,
            )
        )
    return AttemptResultOut(
        attempt_id=attempt.id,
        quiz_id=attempt.quiz_id,
        guest_ref=attempt.guest_ref,
        attempt_number=attempt.attempt_number,
        submitted_at=attempt.submitted_at,
        final_score=_display(attempt.final_score),
        final_pct=_display(attempt.final_pct),
        passed=attempt.passed,
        questions=results,
    )


# ---------- operations ----------


class WrongOptionOut(BaseModel):
    option_id: str
    text: str
    count: int


class MissedQuestionOut(BaseModel):
    question_id: str
    position: int
    text: str
    topic: Topic
    attempts: int
    misses: int
    miss_rate: float
    most_chosen_wrong_option: WrongOptionOut | None


class MissedTopicOut(BaseModel):
    topic: Topic
    attempts: int
    misses: int
    miss_rate: float
    question_count: int


class AttemptSummaryOut(BaseModel):
    attempt_id: str
    quiz_id: str
    building_slug: str
    attempt_number: int
    status: AttemptStatus
    started_at: datetime
    submitted_at: datetime | None
    final_pct: float | None
    passed: bool | None
    missed_topics: list[Topic]


class GuestSummaryOut(BaseModel):
    guest_ref: str
    latest: AttemptSummaryOut = Field(description="Most recent attempt, in any status")
    latest_submitted: AttemptSummaryOut | None = Field(
        description="Most recent submitted attempt: what the front desk acts on"
    )
    history: list[AttemptSummaryOut]


def missed_question_out(stat: QuestionMissStat) -> MissedQuestionOut:
    wrong = stat.most_chosen_wrong_option
    return MissedQuestionOut(
        question_id=stat.question_id,
        position=stat.position,
        text=stat.text,
        topic=stat.topic,
        attempts=stat.attempts,
        misses=stat.misses,
        miss_rate=_display(stat.miss_rate),
        most_chosen_wrong_option=(
            WrongOptionOut(option_id=wrong.option_id, text=wrong.text, count=wrong.count)
            if wrong
            else None
        ),
    )


def missed_topic_out(stat: TopicMissStat) -> MissedTopicOut:
    return MissedTopicOut(
        topic=stat.topic,
        attempts=stat.attempts,
        misses=stat.misses,
        miss_rate=_display(stat.miss_rate),
        question_count=stat.question_count,
    )


def attempt_summary_out(attempt: Attempt) -> AttemptSummaryOut:
    submitted = attempt.status is AttemptStatus.SUBMITTED
    return AttemptSummaryOut(
        attempt_id=attempt.id,
        quiz_id=attempt.quiz_id,
        building_slug=attempt.quiz.building.slug,
        attempt_number=attempt.attempt_number,
        status=attempt.status,
        started_at=attempt.started_at,
        submitted_at=attempt.submitted_at,
        final_pct=_display(attempt.final_pct) if attempt.final_pct is not None else None,
        passed=attempt.passed,
        missed_topics=missed_topics_for_attempt(attempt) if submitted else [],
    )
