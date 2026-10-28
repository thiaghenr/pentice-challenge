import pytest

from pentice_quiz.domain.schemas import LogStatus, Section, Strategy, Topic
from pentice_quiz.domain.validation import validate_candidates
from pentice_quiz.llm.client import LLMError, LLMTimeoutError
from pentice_quiz.services.fallback import (
    build_template_questions,
    infer_topic,
    numeric_variants,
)
from pentice_quiz.services.generation import QuizAgent
from tests.conftest import FakeLLMClient, as_llm_reply, llm_questions, load_llm_output

HARBOR = "harbor_lofts_valid.json"


class TestNumericVariants:
    def test_twelve_hour_times(self) -> None:
        assert numeric_variants("4:00 PM") == ["5:00 PM", "3:00 PM", "6:00 PM"]

    def test_noon_and_midnight_wrap(self) -> None:
        assert numeric_variants("11:00 AM") == ["12:00 PM", "10:00 AM", "1:00 PM"]
        assert numeric_variants("11:00 PM")[0] == "12:00 AM"

    def test_twenty_four_hour_times(self) -> None:
        assert numeric_variants("23:30") == ["00:30", "22:30", "01:30"]

    def test_money_keeps_its_unit(self) -> None:
        assert numeric_variants("$40 per hour") == ["$80 per hour", "$20 per hour", "$60 per hour"]

    def test_decimals_keep_precision(self) -> None:
        assert numeric_variants("2.0 m") == ["1.5 m", "2.5 m", "3.0 m"]

    def test_small_numbers(self) -> None:
        assert len(numeric_variants("1 key")) == 3

    def test_no_number(self) -> None:
        assert numeric_variants("two hours before check-in") == []


def test_topic_inference_prefers_the_heading() -> None:
    assert infer_topic("Parking", "A car without a booking is towed.") is Topic.PARKING
    assert infer_topic("Money", "Late check-out costs $50.") is Topic.CHECK_IN
    assert infer_topic("Misc", "Bring sunscreen.") is Topic.OTHER


@pytest.mark.parametrize("sections_fixture", ["harbor_sections", "pine_sections"])
def test_template_questions_are_valid_and_grounded(
    sections_fixture: str, request: pytest.FixtureRequest
) -> None:
    sections: list[Section] = request.getfixturevalue(sections_fixture)
    questions = build_template_questions(sections, 8)
    outcome = validate_candidates(
        [q.model_dump(mode="json") for q in questions], sections, max_per_section=2
    )
    assert outcome.errors == []
    assert 5 <= len(outcome.valid) <= 8
    for question in outcome.valid:
        assert sum(o.is_correct for o in question.options) == 1
        assert "_____" in question.text


def test_template_covers_sections_round_robin(harbor_sections: list[Section]) -> None:
    # 7 Harbor sections have highlighted numbers: all are used before any repeats.
    questions = build_template_questions(harbor_sections, 8)
    assert len({q.section for q in questions[:7]}) == 7
    assert questions[7].section == questions[0].section


def test_template_is_deterministic(pine_sections: list[Section]) -> None:
    assert build_template_questions(pine_sections, 6) == build_template_questions(pine_sections, 6)


def test_template_gives_nothing_without_highlighted_values() -> None:
    sections = [Section("Noise", "Please be quiet at night."), Section("Pets", "No pets.")]
    assert build_template_questions(sections, 6) == []


class TestQuizAgent:
    def _agent(self, *clients: FakeLLMClient, sleeps: list[float] | None = None) -> QuizAgent:
        recorded = sleeps if sleeps is not None else []
        return QuizAgent(list(clients), max_retries=2, sleep=recorded.append)

    def test_valid_reply_on_first_call(self, harbor_sections: list[Section]) -> None:
        llm = FakeLLMClient(replies=[load_llm_output(HARBOR)])
        result = self._agent(llm).generate(
            harbor_sections, num_questions=6, strategy=Strategy.BY_RISK
        )
        assert len(result.questions) == 6
        assert (result.provider, result.model) == ("fake", "fake-model")
        assert [log.status for log in result.logs] == [LogStatus.OK]

    def test_by_section_caps_two_per_section(self, harbor_sections: list[Section]) -> None:
        questions = llm_questions(HARBOR)
        extra = dict(questions[0], text="Can you check in before 4:00 PM without confirmation?")
        llm = FakeLLMClient(replies=[as_llm_reply([*questions, extra])])
        result = self._agent(llm).generate(
            harbor_sections, num_questions=8, strategy=Strategy.BY_SECTION
        )
        sections = [q.section for q in result.questions]
        assert sections.count("Check-in and check-out") == 2

    def test_timeout_then_success_backs_off(self, harbor_sections: list[Section]) -> None:
        sleeps: list[float] = []
        llm = FakeLLMClient(replies=[LLMTimeoutError("slow"), load_llm_output(HARBOR)])
        result = self._agent(llm, sleeps=sleeps).generate(
            harbor_sections, num_questions=6, strategy=Strategy.BY_SECTION
        )
        assert len(result.questions) == 6
        assert [log.status for log in result.logs] == [LogStatus.TIMEOUT, LogStatus.OK]
        assert sleeps == [0.5]

    def test_repair_prompt_asks_only_for_missing_questions(
        self, harbor_sections: list[Section]
    ) -> None:
        questions = llm_questions(HARBOR)
        broken = [*questions[:3], dict(questions[3], source_quote="Quiet from 9 PM.")]
        llm = FakeLLMClient(replies=[as_llm_reply(broken), as_llm_reply(questions[3:6])])
        result = self._agent(llm).generate(
            harbor_sections, num_questions=6, strategy=Strategy.BY_SECTION
        )
        assert len(result.questions) == 6
        assert [log.status for log in result.logs] == [LogStatus.INVALID, LogStatus.OK]
        repair = llm.calls[1][-1]["content"]
        assert "exactly 3 NEW questions" in repair
        assert "verbatim" in repair
        assert llm.calls[1][-2]["role"] == "assistant"

    def test_falls_through_to_secondary_model(self, harbor_sections: list[Section]) -> None:
        primary = FakeLLMClient(
            replies=["not json", "still not json", LLMError("boom")], provider="openai"
        )
        secondary = FakeLLMClient(replies=[load_llm_output(HARBOR)], provider="openrouter")
        result = self._agent(primary, secondary).generate(
            harbor_sections, num_questions=6, strategy=Strategy.BY_SECTION
        )
        assert result.provider == "openrouter"
        assert [log.status for log in result.logs] == [
            LogStatus.INVALID,
            LogStatus.INVALID,
            LogStatus.ERROR,
            LogStatus.OK,
        ]

    def test_everything_fails(self, harbor_sections: list[Section]) -> None:
        llm = FakeLLMClient(replies=[LLMTimeoutError("t")] * 3)
        result = self._agent(llm).generate(
            harbor_sections, num_questions=6, strategy=Strategy.BY_SECTION
        )
        assert result.questions == []
        assert result.provider is None
        assert len(result.logs) == 3

    def test_no_clients_configured(self, harbor_sections: list[Section]) -> None:
        result = QuizAgent([]).generate(
            harbor_sections, num_questions=6, strategy=Strategy.BY_SECTION
        )
        assert result.questions == []

    def test_past_mistakes_are_in_the_prompt(self, harbor_sections: list[Section]) -> None:
        llm = FakeLLMClient(replies=[load_llm_output(HARBOR)])
        self._agent(llm).generate(
            harbor_sections,
            num_questions=6,
            strategy=Strategy.BY_PAST_MISTAKES,
            topic_miss_rates={Topic.PARKING: 0.6, Topic.NOISE: 0.2},
        )
        assert "parking (60% missed), noise (20% missed)" in llm.calls[0][1]["content"]
