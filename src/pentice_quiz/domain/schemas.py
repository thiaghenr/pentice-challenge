from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

OPTIONS_PER_QUESTION = 4
MIN_CORRECT_OPTIONS = 1
MAX_CORRECT_OPTIONS = 3
MIN_QUESTIONS = 5
MAX_QUESTIONS = 8
MAX_QUESTIONS_PER_SECTION = 2


class Topic(StrEnum):
    CHECK_IN = "check_in"
    PARKING = "parking"
    NOISE = "noise"
    DEPOSIT = "deposit"
    PETS = "pets"
    SMOKING = "smoking"
    VISITORS = "visitors"
    AMENITIES = "amenities"
    WASTE = "waste"
    EMERGENCY = "emergency"
    OTHER = "other"


class Strategy(StrEnum):
    BY_SECTION = "by_section"
    BY_RISK = "by_risk"
    BY_PAST_MISTAKES = "by_past_mistakes"


class GenerationMode(StrEnum):
    LLM = "llm"
    FALLBACK_CACHE = "fallback_cache"
    FALLBACK_TEMPLATE = "fallback_template"


class QuizStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class AttemptStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"


class LogStatus(StrEnum):
    OK = "ok"
    INVALID = "invalid"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Section:
    heading: str
    text: str


class GeneratedOption(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=200)
    is_correct: bool


class GeneratedQuestion(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    text: str = Field(min_length=10, max_length=300)
    section: str = Field(min_length=1, max_length=200)
    source_quote: str = Field(min_length=1, max_length=600)
    topic: Topic
    explanation: str = Field(min_length=1, max_length=500)
    options: list[GeneratedOption]

    @field_validator("topic", mode="before")
    @classmethod
    def _normalize_topic(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower().replace("-", "_").replace(" ", "_")
        return value

    @field_validator("options")
    @classmethod
    def _check_options(cls, options: list[GeneratedOption]) -> list[GeneratedOption]:
        if len(options) != OPTIONS_PER_QUESTION:
            raise ValueError(f"expected exactly {OPTIONS_PER_QUESTION} options, got {len(options)}")
        if len({option.text.casefold() for option in options}) != len(options):
            raise ValueError("option texts must be unique")
        correct = sum(option.is_correct for option in options)
        if not MIN_CORRECT_OPTIONS <= correct <= MAX_CORRECT_OPTIONS:
            raise ValueError(
                f"expected {MIN_CORRECT_OPTIONS} to {MAX_CORRECT_OPTIONS} correct options, "
                f"got {correct}"
            )
        return options

    @property
    def multi_select(self) -> bool:
        return sum(option.is_correct for option in self.options) > 1
