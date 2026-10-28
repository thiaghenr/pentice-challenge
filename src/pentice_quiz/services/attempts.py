from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pentice_quiz.db.models import Answer, Attempt, Question, Quiz, utcnow
from pentice_quiz.domain.schemas import AttemptStatus, QuizStatus
from pentice_quiz.domain.scoring import (
    MAX_QUESTION_SCORE,
    is_passed,
    question_weights,
    score_question,
    to_percentage,
    weighted_final_score,
)
from pentice_quiz.errors import ConflictError, InvalidRequestError, NotFoundError


def start_attempt(session: Session, quiz_id: str, guest_ref: str) -> tuple[Attempt, bool]:
    quiz = session.get(Quiz, quiz_id)
    if quiz is None or quiz.status is not QuizStatus.PUBLISHED:
        raise NotFoundError(f"quiz {quiz_id} not found", code="quiz_not_found")

    latest = _latest_attempt(session, quiz_id, guest_ref)
    if latest is not None and latest.status is AttemptStatus.IN_PROGRESS:
        return latest, False

    attempt = Attempt(
        quiz_id=quiz_id,
        guest_ref=guest_ref,
        attempt_number=latest.attempt_number + 1 if latest else 1,
        status=AttemptStatus.IN_PROGRESS,
        question_order_json=[question.id for question in quiz.questions],
        previous_attempt_id=latest.id if latest else None,
    )
    session.add(attempt)
    try:
        session.commit()
    except IntegrityError:
        # A concurrent request created the same attempt first: return that one.
        session.rollback()
        winner = _latest_attempt(session, quiz_id, guest_ref)
        if winner is None or winner.status is not AttemptStatus.IN_PROGRESS:
            raise
        return winner, False
    return attempt, True


def get_attempt(session: Session, attempt_id: str) -> Attempt:
    attempt = session.get(Attempt, attempt_id)
    if attempt is None:
        raise NotFoundError(f"attempt {attempt_id} not found", code="attempt_not_found")
    return attempt


def save_answer(
    session: Session, attempt_id: str, question_id: str, option_ids: Sequence[str]
) -> Answer:
    attempt = _get_attempt_for_update(session, attempt_id)
    if attempt.status is AttemptStatus.SUBMITTED:
        raise ConflictError("attempt was already submitted", code="attempt_submitted")
    if question_id not in attempt.question_order_json:
        raise NotFoundError(
            f"question {question_id} is not part of this attempt", code="question_not_found"
        )

    question = session.get(Question, question_id)
    if question is None:
        raise NotFoundError(f"question {question_id} not found", code="question_not_found")
    selected = list(dict.fromkeys(option_ids))
    unknown = set(selected) - {option.id for option in question.options}
    if unknown:
        raise InvalidRequestError(
            f"options do not belong to this question: {sorted(unknown)}", code="unknown_option"
        )
    if not question.multi_select and len(selected) > 1:
        raise InvalidRequestError(
            "this question accepts a single option", code="single_select_question"
        )

    answer = session.scalars(
        select(Answer).where(Answer.attempt_id == attempt.id, Answer.question_id == question_id)
    ).first()
    if answer is None:
        answer = Answer(
            attempt_id=attempt.id, question_id=question_id, selected_option_ids_json=selected
        )
        session.add(answer)
    else:
        answer.selected_option_ids_json = selected
        answer.answered_at = utcnow()
    session.commit()
    return answer


def submit_attempt(session: Session, attempt_id: str, *, pass_threshold_pct: float) -> Attempt:
    attempt = _get_attempt_for_update(session, attempt_id)
    if attempt.status is AttemptStatus.SUBMITTED:
        return attempt

    questions = {question.id: question for question in attempt.quiz.questions}
    answers = {answer.question_id: answer for answer in attempt.answers}
    scores: list[float] = []
    for question_id in attempt.question_order_json:
        question = questions[question_id]
        answer = answers.get(question_id)
        if answer is None:
            answer = Answer(question_id=question_id, selected_option_ids_json=[])
            attempt.answers.append(answer)
        correct = {option.id for option in question.options if option.is_correct}
        score = score_question(set(answer.selected_option_ids_json), correct)
        answer.score = score
        answer.is_correct = score == MAX_QUESTION_SCORE
        scores.append(score)

    # Weights follow the order frozen in this attempt (PRD RF4).
    final_score = weighted_final_score(scores, question_weights(len(scores)))
    attempt.final_score = final_score
    attempt.final_pct = to_percentage(final_score)
    attempt.passed = is_passed(attempt.final_pct, pass_threshold_pct)
    attempt.status = AttemptStatus.SUBMITTED
    attempt.submitted_at = utcnow()
    session.commit()
    return attempt


def list_guest_attempts(session: Session, guest_ref: str) -> list[Attempt]:
    attempts = list(
        session.scalars(
            select(Attempt)
            .where(Attempt.guest_ref == guest_ref)
            .order_by(Attempt.started_at.desc(), Attempt.attempt_number.desc())
        )
    )
    if not attempts:
        raise NotFoundError(f"no attempts for guest {guest_ref}", code="guest_not_found")
    return attempts


def _latest_attempt(session: Session, quiz_id: str, guest_ref: str) -> Attempt | None:
    return session.scalars(
        select(Attempt)
        .where(Attempt.quiz_id == quiz_id, Attempt.guest_ref == guest_ref)
        .order_by(Attempt.attempt_number.desc())
        .limit(1)
    ).first()


def _get_attempt_for_update(session: Session, attempt_id: str) -> Attempt:
    attempt = session.scalars(
        select(Attempt).where(Attempt.id == attempt_id).with_for_update()
    ).first()
    if attempt is None:
        raise NotFoundError(f"attempt {attempt_id} not found", code="attempt_not_found")
    return attempt
