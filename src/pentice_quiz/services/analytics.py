from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from pentice_quiz.db.models import Answer, Attempt, Building, Option, Question, Quiz
from pentice_quiz.domain.schemas import AttemptStatus, Topic
from pentice_quiz.domain.scoring import MAX_QUESTION_SCORE
from pentice_quiz.errors import NotFoundError


@dataclass(frozen=True, slots=True)
class WrongOptionStat:
    option_id: str
    text: str
    count: int


@dataclass(frozen=True, slots=True)
class QuestionMissStat:
    question_id: str
    position: int
    text: str
    topic: Topic
    attempts: int
    misses: int
    miss_rate: float
    most_chosen_wrong_option: WrongOptionStat | None


@dataclass(frozen=True, slots=True)
class TopicMissStat:
    topic: Topic
    attempts: int
    misses: int
    miss_rate: float
    question_count: int


def missed_questions(
    session: Session, quiz_id: str, *, min_attempts: int
) -> list[QuestionMissStat]:
    quiz = session.get(Quiz, quiz_id)
    if quiz is None:
        raise NotFoundError(f"quiz {quiz_id} not found", code="quiz_not_found")
    answers_by_question = _submitted_answers_by_question(session, [quiz.id])

    stats: list[QuestionMissStat] = []
    for question in quiz.questions:
        answers = answers_by_question.get(question.id, [])
        if len(answers) < min_attempts:
            continue
        missed = [answer for answer in answers if _is_miss(answer)]
        stats.append(
            QuestionMissStat(
                question_id=question.id,
                position=question.position,
                text=question.text,
                topic=question.topic,
                attempts=len(answers),
                misses=len(missed),
                miss_rate=len(missed) / len(answers),
                most_chosen_wrong_option=_most_chosen_wrong_option(question.options, missed),
            )
        )
    stats.sort(key=lambda stat: (-stat.miss_rate, -stat.misses, stat.position))
    return stats


def missed_topics(
    session: Session, building_slug: str, *, min_attempts: int
) -> list[TopicMissStat]:
    building = session.scalars(select(Building).where(Building.slug == building_slug)).first()
    if building is None:
        raise NotFoundError(f"building {building_slug} not found", code="building_not_found")
    stats = [stat for stat in _topic_stats(session, building.id) if stat.attempts >= min_attempts]
    stats.sort(key=lambda stat: (-stat.miss_rate, -stat.misses, stat.topic.value))
    return stats


def topic_miss_rates(session: Session, building_id: str) -> dict[Topic, float]:
    return {stat.topic: stat.miss_rate for stat in _topic_stats(session, building_id)}


def missed_topics_for_attempt(attempt: Attempt) -> list[Topic]:
    topics = {answer.question.topic for answer in attempt.answers if _is_miss(answer)}
    return sorted(topics, key=lambda topic: topic.value)


def _topic_stats(session: Session, building_id: str) -> list[TopicMissStat]:
    quiz_ids = list(session.scalars(select(Quiz.id).where(Quiz.building_id == building_id)))
    questions = session.scalars(select(Question).where(Question.quiz_id.in_(quiz_ids))).all()
    answers_by_question = _submitted_answers_by_question(session, quiz_ids)

    attempts: Counter[Topic] = Counter()
    misses: Counter[Topic] = Counter()
    question_count: Counter[Topic] = Counter()
    for question in questions:
        answers = answers_by_question.get(question.id, [])
        question_count[question.topic] += 1
        attempts[question.topic] += len(answers)
        misses[question.topic] += sum(1 for answer in answers if _is_miss(answer))

    return [
        TopicMissStat(
            topic=topic,
            attempts=attempts[topic],
            misses=misses[topic],
            miss_rate=misses[topic] / attempts[topic],
            question_count=question_count[topic],
        )
        for topic in question_count
        if attempts[topic] > 0
    ]


def _submitted_answers_by_question(
    session: Session, quiz_ids: Sequence[str]
) -> dict[str, list[Answer]]:
    answers = session.scalars(
        select(Answer)
        .join(Attempt, Answer.attempt_id == Attempt.id)
        .where(Attempt.quiz_id.in_(quiz_ids), Attempt.status == AttemptStatus.SUBMITTED)
    ).all()
    grouped: dict[str, list[Answer]] = defaultdict(list)
    for answer in answers:
        grouped[answer.question_id].append(answer)
    return grouped


def _is_miss(answer: Answer) -> bool:
    return (answer.score or 0.0) < MAX_QUESTION_SCORE


def _most_chosen_wrong_option(
    options: Sequence[Option], missed: Sequence[Answer]
) -> WrongOptionStat | None:
    wrong_ids = {option.id: option for option in options if not option.is_correct}
    counts = Counter(
        option_id
        for answer in missed
        for option_id in answer.selected_option_ids_json
        if option_id in wrong_ids
    )
    if not counts:
        return None
    option_id, count = counts.most_common(1)[0]
    return WrongOptionStat(option_id=option_id, text=wrong_ids[option_id].text, count=count)
