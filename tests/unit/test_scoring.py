import pytest
from hypothesis import given
from hypothesis import strategies as st

from pentice_quiz.domain.scoring import (
    MAX_QUESTION_SCORE,
    is_passed,
    question_weights,
    score_question,
    to_percentage,
    weighted_final_score,
)

OPTIONS = frozenset({"a", "b", "c", "d"})


class TestSingleAnswer:
    def test_exactly_the_correct_option_scores_full(self) -> None:
        assert score_question({"b"}, {"b"}) == 4.0

    def test_wrong_option_scores_zero(self) -> None:
        assert score_question({"a"}, {"b"}) == 0.0

    def test_correct_plus_wrong_option_scores_zero(self) -> None:
        assert score_question({"a", "b"}, {"b"}) == 0.0

    def test_blank_scores_zero(self) -> None:
        assert score_question(set(), {"b"}) == 0.0


class TestMultipleAnswers:
    def test_all_correct_options_score_full(self) -> None:
        assert score_question({"a", "b"}, {"a", "b"}) == 4.0

    def test_partial_selection_is_proportional(self) -> None:
        assert score_question({"a", "b"}, {"a", "b", "c"}) == pytest.approx(4 * 2 / 3)

    def test_wrong_pick_is_penalised(self) -> None:
        # 2 hits - 1 wrong pick = 1 out of 3 correct
        assert score_question({"a", "b", "d"}, {"a", "b", "c"}) == pytest.approx(4 / 3)

    def test_selecting_everything_with_two_correct_scores_zero(self) -> None:
        assert score_question(set(OPTIONS), {"a", "b"}) == 0.0

    def test_selecting_everything_with_three_correct_loses_one_third(self) -> None:
        assert score_question(set(OPTIONS), {"a", "b", "c"}) == pytest.approx(4 * 2 / 3)

    def test_only_wrong_picks_never_go_negative(self) -> None:
        assert score_question({"c", "d"}, {"a", "b"}) == 0.0

    def test_blank_scores_zero(self) -> None:
        assert score_question(set(), {"a", "b"}) == 0.0


def test_question_without_correct_options_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one correct"):
        score_question({"a"}, set())


class TestWeights:
    def test_weights_grow_ten_percent_per_question(self) -> None:
        assert question_weights(4) == pytest.approx([1.0, 1.1, 1.21, 1.331])

    def test_zero_questions(self) -> None:
        assert question_weights(0) == []

    def test_negative_count_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="count"):
            question_weights(-1)


class TestWeightedFinalScore:
    def test_weighted_mean(self) -> None:
        weights = question_weights(3)
        # (1*4 + 1.1*0 + 1.21*4) / 3.31
        assert weighted_final_score([4.0, 0.0, 4.0], weights) == pytest.approx(8.84 / 3.31)

    def test_later_questions_weigh_more(self) -> None:
        weights = question_weights(2)
        first_right = weighted_final_score([4.0, 0.0], weights)
        last_right = weighted_final_score([0.0, 4.0], weights)
        assert last_right > first_right

    def test_all_full_marks(self) -> None:
        assert weighted_final_score([4.0] * 6, question_weights(6)) == pytest.approx(4.0)

    def test_length_mismatch_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="same length"):
            weighted_final_score([4.0], [1.0, 1.1])

    def test_empty_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="without questions"):
            weighted_final_score([], [])

    def test_non_positive_weights_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            weighted_final_score([4.0], [0.0])


class TestPassFail:
    def test_percentage(self) -> None:
        assert to_percentage(3.0) == pytest.approx(75.0)

    def test_threshold_is_inclusive(self) -> None:
        assert is_passed(75.0, 75.0)

    def test_float_noise_at_threshold_still_passes(self) -> None:
        assert is_passed(74.99999999999999, 75.0)

    def test_below_threshold_fails(self) -> None:
        assert not is_passed(74.9, 75.0)


@given(
    selected=st.sets(st.sampled_from(sorted(OPTIONS))),
    correct=st.sets(st.sampled_from(sorted(OPTIONS)), min_size=1, max_size=3),
)
def test_score_is_always_between_zero_and_max(selected: set[str], correct: set[str]) -> None:
    assert 0.0 <= score_question(selected, correct) <= MAX_QUESTION_SCORE


@given(correct=st.sets(st.sampled_from(sorted(OPTIONS)), min_size=1, max_size=3))
def test_selecting_everything_never_beats_the_exact_answer(correct: set[str]) -> None:
    assert score_question(set(OPTIONS), correct) < score_question(correct, correct)


@given(scores=st.lists(st.floats(min_value=0, max_value=4), min_size=1, max_size=8))
def test_final_score_stays_on_scale(scores: list[float]) -> None:
    final = weighted_final_score(scores, question_weights(len(scores)))
    assert min(scores) - 1e-9 <= final <= max(scores) + 1e-9
