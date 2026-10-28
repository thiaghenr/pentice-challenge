import re
import zlib
from collections.abc import Iterator, Sequence

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from pentice_quiz.db.models import Quiz, SourceSnapshot
from pentice_quiz.domain.schemas import (
    MAX_QUESTIONS_PER_SECTION,
    OPTIONS_PER_QUESTION,
    GeneratedOption,
    GeneratedQuestion,
    QuizStatus,
    Section,
    Topic,
)

TEMPLATE_PROMPT_VERSION = "template-v1"

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(*])")
_TIME_12H = re.compile(r"\b(\d{1,2}):(\d{2})\s*([AaPp][Mm])\b")
_TIME_24H = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_BLANK = "_____"

# Checked in order against the section heading first, then the sentence.
_TOPIC_PATTERNS: tuple[tuple[Topic, re.Pattern[str]], ...] = tuple(
    (topic, re.compile(pattern, re.IGNORECASE))
    for topic, pattern in (
        (Topic.SMOKING, r"\bsmok|\bvap"),
        (Topic.PETS, r"\bpets?\b|\bdogs?\b|\bcats?\b|\banimals?\b"),
        (Topic.PARKING, r"\bparking\b|\bcars?\b|\bgarage\b|\bvehicles?\b"),
        (Topic.NOISE, r"\bquiet\b|\bnoise\b"),
        (Topic.DEPOSIT, r"\bdeposits?\b|\bdamages?\b"),
        (Topic.CHECK_IN, r"\bcheck[- ]?(?:in|out)\b|\barrival\b|\bkeys?\b|\bdoor code\b"),
        (Topic.VISITORS, r"\bvisitors?\b|\boccupancy\b|\bwho can stay\b|\bguests\b"),
        (Topic.AMENITIES, r"\bamenit|\bpool\b|\btowels?\b|\bgym\b|\broof\b|\blaundry\b"),
        (Topic.WASTE, r"\btrash\b|\bgarbage\b|\brecycl|\bwaste\b"),
        (Topic.EMERGENCY, r"\bemergenc|\bfire\b|\balarm\b"),
    )
)


def find_cached_quiz(session: Session, *, building_id: str, content_hash: str) -> Quiz | None:
    statement = (
        select(Quiz)
        .join(SourceSnapshot, Quiz.source_snapshot_id == SourceSnapshot.id)
        .where(
            Quiz.building_id == building_id,
            Quiz.status == QuizStatus.PUBLISHED,
            SourceSnapshot.content_hash == content_hash,
        )
        .order_by(Quiz.published_at.desc())
        .limit(1)
    )
    return session.scalars(statement).first()


def infer_topic(heading: str, sentence: str) -> Topic:
    for text in (heading, sentence):
        for topic, pattern in _TOPIC_PATTERNS:
            if pattern.search(text):
                return topic
    return Topic.OTHER


def numeric_variants(value: str, count: int = OPTIONS_PER_QUESTION - 1) -> list[str]:
    match = _TIME_12H.search(value)
    if match:
        hour = int(match.group(1)) % 12 + (12 if match.group(3).upper() == "PM" else 0)
        replacements = [
            _format_12h((hour + delta) % 24, match.group(2)) for delta in (1, -1, 2, -2, 3)
        ]
    elif match := _TIME_24H.search(value):
        hour = int(match.group(1))
        replacements = [
            f"{(hour + delta) % 24:02d}:{match.group(2)}" for delta in (1, -1, 2, -2, 3)
        ]
    elif match := _NUMBER.search(value):
        replacements = _number_replacements(match.group())
    else:
        return []

    variants: list[str] = []
    for replacement in replacements:
        candidate = value[: match.start()] + replacement + value[match.end() :]
        if candidate != value and candidate not in variants:
            variants.append(candidate)
    return variants[:count]


def build_template_questions(
    sections: Sequence[Section], num_questions: int
) -> list[GeneratedQuestion]:
    per_section = [list(_section_questions(section)) for section in sections]
    picked: list[GeneratedQuestion] = []
    for round_index in range(MAX_QUESTIONS_PER_SECTION):
        for candidates in per_section:
            if len(picked) >= num_questions:
                return picked
            if round_index < len(candidates):
                picked.append(candidates[round_index])
    return picked


def _section_questions(section: Section) -> Iterator[GeneratedQuestion]:
    for sentence in _sentences(section.text):
        # One question per sentence, so two questions never give each other away.
        value_match = next(
            (match for match in _BOLD.finditer(sentence) if any(c.isdigit() for c in match[1])),
            None,
        )
        if value_match is None:
            continue
        value = value_match.group(1).strip()
        distractors = numeric_variants(value)
        if len(distractors) < OPTIONS_PER_QUESTION - 1:
            continue
        plain_sentence = _plain(sentence)
        blanked = _plain(sentence[: value_match.start()] + _BLANK + sentence[value_match.end() :])
        options = list(distractors)
        options.insert(zlib.crc32(value.encode()) % OPTIONS_PER_QUESTION, value)
        try:
            yield GeneratedQuestion(
                text=f"Fill in the blank: {blanked}",
                section=section.heading,
                source_quote=plain_sentence,
                topic=infer_topic(section.heading, plain_sentence),
                explanation=f'The house rules say: "{plain_sentence}"',
                options=[
                    GeneratedOption(text=option, is_correct=option == value) for option in options
                ],
            )
        except ValidationError:
            continue


def _sentences(text: str) -> Iterator[str]:
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or _LIST_ITEM.match(line):
            if current:
                blocks.append(" ".join(current))
            current = [_LIST_ITEM.sub("", line).strip()] if stripped else []
        else:
            current.append(stripped)
    if current:
        blocks.append(" ".join(current))
    for block in blocks:
        yield from (part.strip() for part in _SENTENCE_BREAK.split(block) if part.strip())


def _plain(text: str) -> str:
    return text.replace("**", "")


def _format_12h(hour: int, minutes: str) -> str:
    suffix = "PM" if hour >= 12 else "AM"
    return f"{hour % 12 or 12}:{minutes} {suffix}"


def _number_replacements(raw: str) -> list[str]:
    if "." in raw:
        decimals = len(raw.split(".")[1])
        number = float(raw)
        return [f"{number * factor:.{decimals}f}" for factor in (0.75, 1.25, 1.5, 0.5)]
    number = int(raw)
    candidates = [number * 2, number // 2, number + max(1, round(number / 2)), number * 3]
    candidates += [number + 1, number + 2, number + 3]
    return [str(candidate) for candidate in candidates if candidate > 0]
