import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from pydantic import ValidationError

from pentice_quiz.domain.schemas import GeneratedQuestion, Section

MIN_QUOTE_CHARS = 12
DUPLICATE_SIMILARITY = 0.9

_LIST_MARKER = re.compile(r"(?m)^\s*(?:[-*+]|\d+\.)\s+")
_EMPHASIS = re.compile(r"[*_`]{1,2}")
_WHITESPACE = re.compile(r"\s+")
_CODE_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
# Curly quotes, en/em dashes and non-breaking spaces become their ASCII forms.
_TYPOGRAPHY = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u00a0": " ",
    }
)
_QUOTE_EDGES = " \"'.\u2026"


@dataclass
class ValidationOutcome:
    valid: list[GeneratedQuestion] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def normalize_text(text: str) -> str:
    text = text.translate(_TYPOGRAPHY)
    text = _LIST_MARKER.sub("", text)
    text = _EMPHASIS.sub("", text)
    return _WHITESPACE.sub(" ", text).strip().casefold()


def normalize_heading(heading: str) -> str:
    return normalize_text(heading.lstrip("#"))


def is_grounded(quote: str, normalized_source: str) -> bool:
    normalized_quote = normalize_text(quote).strip(_QUOTE_EDGES)
    return len(normalized_quote) >= MIN_QUOTE_CHARS and normalized_quote in normalized_source


def parse_llm_json(raw: str) -> list[object]:
    fenced = _CODE_FENCE.match(raw)
    if fenced:
        raw = fenced.group(1)
    data = json.loads(raw)
    if isinstance(data, dict):
        data = data.get("questions")
    if not isinstance(data, list):
        raise ValueError('expected a JSON object with a "questions" array')
    return data


def validate_llm_output(
    raw: str,
    sections: Sequence[Section],
    *,
    existing: Sequence[GeneratedQuestion] = (),
    max_per_section: int | None = None,
) -> ValidationOutcome:
    try:
        items = parse_llm_json(raw)
    except ValueError as exc:
        return ValidationOutcome(errors=[f"response is not valid quiz JSON: {exc}"])
    return validate_candidates(items, sections, existing=existing, max_per_section=max_per_section)


def validate_candidates(
    items: Sequence[object],
    sections: Sequence[Section],
    *,
    existing: Sequence[GeneratedQuestion] = (),
    max_per_section: int | None = None,
) -> ValidationOutcome:
    sections_by_heading = {normalize_heading(section.heading): section for section in sections}
    normalized_bodies = {section.heading: normalize_text(section.text) for section in sections}
    accepted = list(existing)
    outcome = ValidationOutcome()

    for index, item in enumerate(items, start=1):
        label = f"question {index}"
        try:
            question = GeneratedQuestion.model_validate(item)
        except ValidationError as exc:
            outcome.errors.append(f"{label}: {_describe(exc)}")
            continue

        section = sections_by_heading.get(normalize_heading(question.section))
        if section is None:
            outcome.errors.append(f"{label}: unknown section {question.section!r}")
            continue
        if not is_grounded(question.source_quote, normalized_bodies[section.heading]):
            outcome.errors.append(
                f"{label}: source_quote is not a verbatim excerpt of section {section.heading!r}"
            )
            continue
        if max_per_section is not None and (
            sum(1 for other in accepted if other.section == section.heading) >= max_per_section
        ):
            outcome.errors.append(
                f"{label}: section {section.heading!r} already has {max_per_section} questions"
            )
            continue
        if _is_duplicate(question.text, accepted):
            outcome.errors.append(f"{label}: duplicates another question")
            continue

        question = question.model_copy(update={"section": section.heading})
        accepted.append(question)
        outcome.valid.append(question)

    return outcome


def _is_duplicate(text: str, accepted: Sequence[GeneratedQuestion]) -> bool:
    normalized = normalize_text(text)
    return any(
        SequenceMatcher(None, normalized, normalize_text(other.text)).ratio()
        >= DUPLICATE_SIMILARITY
        for other in accepted
    )


def _describe(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors()[:5]:
        location = ".".join(str(part) for part in error["loc"]) or "question"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)
