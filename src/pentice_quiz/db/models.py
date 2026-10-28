from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.engine import Connection, Dialect
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Mapper,
    mapped_column,
    object_session,
    relationship,
)
from sqlalchemy.types import TypeDecorator

from pentice_quiz.domain.schemas import (
    AttemptStatus,
    GenerationMode,
    LogStatus,
    QuizStatus,
    Strategy,
    Topic,
)
from pentice_quiz.errors import ImmutableRecordError


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetimes are not allowed; use UTC")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _enum_column(enum_cls: type[StrEnum]) -> Enum:
    return Enum(
        enum_cls,
        native_enum=False,
        length=32,
        values_callable=lambda members: [member.value for member in members],
    )


class Base(DeclarativeBase):
    pass


class Building(Base):
    __tablename__ = "building"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    source_url: Mapped[str | None] = mapped_column(String(2000))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class SourceSnapshot(Base):
    __tablename__ = "source_snapshot"
    __table_args__ = (Index("ix_source_snapshot_building_hash", "building_id", "content_hash"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    building_id: Mapped[str] = mapped_column(ForeignKey("building.id"))
    url: Mapped[str] = mapped_column(String(2000))
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    etag: Mapped[str | None] = mapped_column(String(200))
    last_modified: Mapped[str | None] = mapped_column(String(100))
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Quiz(Base):
    __tablename__ = "quiz"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    building_id: Mapped[str] = mapped_column(ForeignKey("building.id"), index=True)
    source_snapshot_id: Mapped[str] = mapped_column(ForeignKey("source_snapshot.id"))
    status: Mapped[QuizStatus] = mapped_column(_enum_column(QuizStatus))
    strategy: Mapped[Strategy] = mapped_column(_enum_column(Strategy))
    llm_provider: Mapped[str | None] = mapped_column(String(50))
    llm_model: Mapped[str | None] = mapped_column(String(200))
    prompt_version: Mapped[str | None] = mapped_column(String(50))
    generation_mode: Mapped[GenerationMode] = mapped_column(_enum_column(GenerationMode))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    building: Mapped[Building] = relationship()
    source_snapshot: Mapped[SourceSnapshot] = relationship()
    questions: Mapped[list[Question]] = relationship(
        back_populates="quiz", order_by="Question.position", cascade="all, delete-orphan"
    )


class Question(Base):
    __tablename__ = "question"
    __table_args__ = (UniqueConstraint("quiz_id", "position"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    quiz_id: Mapped[str] = mapped_column(ForeignKey("quiz.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    topic: Mapped[Topic] = mapped_column(_enum_column(Topic))
    section: Mapped[str] = mapped_column(String(200))
    source_quote: Mapped[str] = mapped_column(Text)
    explanation: Mapped[str] = mapped_column(Text)
    multi_select: Mapped[bool] = mapped_column(Boolean)
    weight: Mapped[float] = mapped_column(Float)

    quiz: Mapped[Quiz] = relationship(back_populates="questions")
    options: Mapped[list[Option]] = relationship(
        back_populates="question", order_by="Option.position", cascade="all, delete-orphan"
    )


class Option(Base):
    __tablename__ = "option"
    __table_args__ = (UniqueConstraint("question_id", "position"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    question_id: Mapped[str] = mapped_column(ForeignKey("question.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    is_correct: Mapped[bool] = mapped_column(Boolean)

    question: Mapped[Question] = relationship(back_populates="options")


class Attempt(Base):
    __tablename__ = "attempt"
    __table_args__ = (UniqueConstraint("quiz_id", "guest_ref", "attempt_number"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    quiz_id: Mapped[str] = mapped_column(ForeignKey("quiz.id"), index=True)
    guest_ref: Mapped[str] = mapped_column(String(200), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[AttemptStatus] = mapped_column(_enum_column(AttemptStatus))
    question_order_json: Mapped[list[str]] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    final_score: Mapped[float | None] = mapped_column(Float)
    final_pct: Mapped[float | None] = mapped_column(Float)
    passed: Mapped[bool | None] = mapped_column(Boolean)
    previous_attempt_id: Mapped[str | None] = mapped_column(ForeignKey("attempt.id"))

    quiz: Mapped[Quiz] = relationship()
    answers: Mapped[list[Answer]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan"
    )


class Answer(Base):
    __tablename__ = "answer"
    __table_args__ = (UniqueConstraint("attempt_id", "question_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("attempt.id"), index=True)
    question_id: Mapped[str] = mapped_column(ForeignKey("question.id"), index=True)
    selected_option_ids_json: Mapped[list[str]] = mapped_column(JSON)
    score: Mapped[float | None] = mapped_column(Float)
    is_correct: Mapped[bool | None] = mapped_column(Boolean)
    answered_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    attempt: Mapped[Attempt] = relationship(back_populates="answers")
    question: Mapped[Question] = relationship()


class LlmGenerationLog(Base):
    __tablename__ = "llm_generation_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    quiz_id: Mapped[str | None] = mapped_column(ForeignKey("quiz.id"), index=True)
    building_id: Mapped[str | None] = mapped_column(ForeignKey("building.id"))
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(200))
    prompt_version: Mapped[str] = mapped_column(String(50))
    latency_ms: Mapped[int] = mapped_column(Integer)
    status: Mapped[LogStatus] = mapped_column(_enum_column(LogStatus))
    validation_errors_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


def _reject_update(mapper: Mapper[Any], connection: Connection, target: Any) -> None:
    session = object_session(target)
    if session is not None and session.is_modified(target, include_collections=False):
        raise ImmutableRecordError(f"{type(target).__name__} rows cannot change once published")


event.listen(Question, "before_update", _reject_update)
event.listen(Option, "before_update", _reject_update)
