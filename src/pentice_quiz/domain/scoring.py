from collections.abc import Hashable, Sequence
from collections.abc import Set as AbstractSet

MAX_QUESTION_SCORE = 4.0
WEIGHT_GROWTH = 1.1
# Guards the pass/fail comparison against float noise (e.g. 74.99999999999999).
_PASS_EPSILON = 1e-9


def score_question[OptionId: Hashable](
    selected: AbstractSet[OptionId], correct: AbstractSet[OptionId]
) -> float:
    if not correct:
        raise ValueError("a question needs at least one correct option")
    if not selected:
        return 0.0
    if len(correct) == 1:
        return MAX_QUESTION_SCORE if selected == correct else 0.0
    hits = len(selected & correct)
    wrong_picks = len(selected - correct)
    return MAX_QUESTION_SCORE * max(0, hits - wrong_picks) / len(correct)


def question_weights(count: int) -> list[float]:
    if count < 0:
        raise ValueError("count must be >= 0")
    return [WEIGHT_GROWTH**index for index in range(count)]


def weighted_final_score(scores: Sequence[float], weights: Sequence[float]) -> float:
    if len(scores) != len(weights):
        raise ValueError("scores and weights must have the same length")
    if not scores:
        raise ValueError("cannot compute a final score without questions")
    total_weight = sum(weights)
    if total_weight <= 0:
        raise ValueError("weights must sum to a positive number")
    return sum(w * s for w, s in zip(weights, scores, strict=True)) / total_weight


def to_percentage(final_score: float) -> float:
    return final_score / MAX_QUESTION_SCORE * 100


def is_passed(final_pct: float, threshold_pct: float) -> bool:
    return final_pct >= threshold_pct - _PASS_EPSILON
