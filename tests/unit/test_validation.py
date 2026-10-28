from typing import Any

import pytest

from pentice_quiz.domain.schemas import Section
from pentice_quiz.domain.validation import (
    is_grounded,
    normalize_text,
    parse_llm_json,
    validate_candidates,
    validate_llm_output,
)
from tests.conftest import as_llm_reply, llm_questions, load_llm_output

HARBOR = "harbor_lofts_valid.json"


def _validate_one(question: dict[str, Any], sections: list[Section]) -> list[str]:
    outcome = validate_candidates([question], sections)
    assert not outcome.valid
    return outcome.errors


class TestNormalization:
    def test_strips_emphasis_bullets_whitespace_and_case(self) -> None:
        assert normalize_text("- Check-in opens at **4:00 PM**.\n\n  Early") == (
            "check-in opens at 4:00 pm. early"
        )

    def test_typographic_quotes_and_dashes(self) -> None:
        assert normalize_text("\u201cdon\u2019t\u201d \u2013 ok") == '"don\'t" - ok'

    def test_grounded_quote_ignores_formatting(self) -> None:
        source = normalize_text("- Quiet hours are **10:00 PM to 8:00 AM** every day.")
        assert is_grounded("quiet hours are 10:00 PM to 8:00 AM", source)

    def test_paraphrase_is_not_grounded(self) -> None:
        source = normalize_text("Quiet hours are **10:00 PM to 8:00 AM** every day.")
        assert not is_grounded("Quiet time starts at 10 PM", source)

    def test_too_short_quote_is_not_grounded(self) -> None:
        assert not is_grounded("PM", normalize_text("Check-in opens at 4:00 PM."))


class TestParsing:
    def test_accepts_code_fenced_json(self) -> None:
        assert parse_llm_json('```json\n{"questions": []}\n```') == []

    def test_accepts_bare_array(self) -> None:
        assert parse_llm_json("[1]") == [1]

    @pytest.mark.parametrize("raw", ['{"items": []}', '"text"', "42"])
    def test_rejects_wrong_shape(self, raw: str) -> None:
        with pytest.raises(ValueError, match="questions"):
            parse_llm_json(raw)


class TestLlmOutput:
    def test_valid_fixture_passes_completely(self, harbor_sections: list[Section]) -> None:
        outcome = validate_llm_output(load_llm_output(HARBOR), harbor_sections)
        assert outcome.errors == []
        assert len(outcome.valid) == 7
        assert [q.multi_select for q in outcome.valid[:2]] == [False, True]

    def test_truncated_json_is_reported_not_raised(self, harbor_sections: list[Section]) -> None:
        outcome = validate_llm_output(load_llm_output("truncated.json"), harbor_sections)
        assert outcome.valid == []
        assert "not valid quiz JSON" in outcome.errors[0]

    def test_non_json_prose(self, harbor_sections: list[Section]) -> None:
        outcome = validate_llm_output("Sure! Here is your quiz:", harbor_sections)
        assert outcome.valid == []
        assert outcome.errors

    def test_bad_question_is_dropped_individually(self, harbor_sections: list[Section]) -> None:
        questions = llm_questions(HARBOR)
        questions[1]["source_quote"] = "Late check-out is always free of charge."
        outcome = validate_llm_output(as_llm_reply(questions), harbor_sections)
        assert len(outcome.valid) == 6
        assert len(outcome.errors) == 1
        assert outcome.errors[0].startswith("question 2:")

    def test_section_is_canonicalised(self, harbor_sections: list[Section]) -> None:
        questions = llm_questions(HARBOR)[:1]
        questions[0]["section"] = "## CHECK-IN AND CHECK-OUT"
        outcome = validate_llm_output(as_llm_reply(questions), harbor_sections)
        assert outcome.valid[0].section == "Check-in and check-out"


class TestStructuralRules:
    @pytest.fixture
    def question(self) -> dict[str, Any]:
        return llm_questions(HARBOR)[0]

    def test_three_options(self, question: dict[str, Any], harbor_sections: list[Section]) -> None:
        question["options"] = question["options"][:3]
        assert "exactly 4 options" in _validate_one(question, harbor_sections)[0]

    def test_five_options(self, question: dict[str, Any], harbor_sections: list[Section]) -> None:
        question["options"].append({"text": "6:00 PM", "is_correct": False})
        assert "exactly 4 options" in _validate_one(question, harbor_sections)[0]

    def test_zero_correct(self, question: dict[str, Any], harbor_sections: list[Section]) -> None:
        for option in question["options"]:
            option["is_correct"] = False
        assert "got 0" in _validate_one(question, harbor_sections)[0]

    def test_all_four_correct(
        self, question: dict[str, Any], harbor_sections: list[Section]
    ) -> None:
        for option in question["options"]:
            option["is_correct"] = True
        assert "got 4" in _validate_one(question, harbor_sections)[0]

    def test_duplicate_option_texts(
        self, question: dict[str, Any], harbor_sections: list[Section]
    ) -> None:
        question["options"][1]["text"] = question["options"][0]["text"].upper()
        assert "unique" in _validate_one(question, harbor_sections)[0]

    def test_empty_option_text(
        self, question: dict[str, Any], harbor_sections: list[Section]
    ) -> None:
        question["options"][0]["text"] = "   "
        assert _validate_one(question, harbor_sections)

    def test_missing_field(self, question: dict[str, Any], harbor_sections: list[Section]) -> None:
        del question["source_quote"]
        assert "source_quote" in _validate_one(question, harbor_sections)[0]

    def test_unknown_topic(self, question: dict[str, Any], harbor_sections: list[Section]) -> None:
        question["topic"] = "spa"
        assert "topic" in _validate_one(question, harbor_sections)[0]

    def test_topic_is_normalised(
        self, question: dict[str, Any], harbor_sections: list[Section]
    ) -> None:
        question["topic"] = " Check-In "
        assert validate_candidates([question], harbor_sections).valid[0].topic == "check_in"

    def test_not_an_object(self, harbor_sections: list[Section]) -> None:
        assert _validate_one("just a string", harbor_sections)  # type: ignore[arg-type]


class TestGrounding:
    @pytest.fixture
    def question(self) -> dict[str, Any]:
        return llm_questions(HARBOR)[0]

    def test_unknown_section(
        self, question: dict[str, Any], harbor_sections: list[Section]
    ) -> None:
        question["section"] = "Spa and wellness"
        assert "unknown section" in _validate_one(question, harbor_sections)[0]

    def test_fabricated_quote(
        self, question: dict[str, Any], harbor_sections: list[Section]
    ) -> None:
        question["source_quote"] = "Check-in opens at 2:00 PM sharp."
        assert "verbatim" in _validate_one(question, harbor_sections)[0]

    def test_quote_from_another_section(
        self, question: dict[str, Any], harbor_sections: list[Section]
    ) -> None:
        question["source_quote"] = "Quiet hours are 10:00 PM to 8:00 AM every day."
        assert "verbatim" in _validate_one(question, harbor_sections)[0]

    def test_quote_from_other_building(
        self, question: dict[str, Any], harbor_sections: list[Section]
    ) -> None:
        question["source_quote"] = "Check-in is from **3:00 PM**."
        assert "verbatim" in _validate_one(question, harbor_sections)[0]


class TestBatchRules:
    def test_duplicate_questions(self, harbor_sections: list[Section]) -> None:
        first = llm_questions(HARBOR)[0]
        second = llm_questions(HARBOR)[0]
        second["text"] = "What time does check-in open at Harbor Lofts ?"
        outcome = validate_candidates([first, second], harbor_sections)
        assert len(outcome.valid) == 1
        assert "duplicates" in outcome.errors[0]

    def test_duplicates_of_previously_accepted(self, harbor_sections: list[Section]) -> None:
        accepted = validate_candidates(llm_questions(HARBOR)[:1], harbor_sections).valid
        outcome = validate_candidates(llm_questions(HARBOR)[:1], harbor_sections, existing=accepted)
        assert outcome.valid == []

    def test_max_per_section(self, harbor_sections: list[Section]) -> None:
        questions = llm_questions(HARBOR)[:2]  # both from "Check-in and check-out"
        outcome = validate_candidates(questions, harbor_sections, max_per_section=1)
        assert len(outcome.valid) == 1
        assert "already has 1" in outcome.errors[0]


def test_pine_court_fixture_is_valid(pine_sections: list[Section]) -> None:
    outcome = validate_llm_output(load_llm_output("pine_court_valid.json"), pine_sections)
    assert outcome.errors == []
    assert len(outcome.valid) == 6
