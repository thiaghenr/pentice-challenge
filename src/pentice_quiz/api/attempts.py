from fastapi import APIRouter, Response, status

from pentice_quiz.api.deps import SessionDep, SettingsDep
from pentice_quiz.api.schemas import (
    AnswerOut,
    AttemptOut,
    AttemptResultOut,
    SaveAnswerIn,
    StartAttemptIn,
    attempt_out,
    attempt_result_out,
)
from pentice_quiz.domain.schemas import AttemptStatus
from pentice_quiz.errors import ConflictError
from pentice_quiz.services import attempts as attempt_service

router = APIRouter(tags=["attempts"])


@router.post(
    "/quizzes/{quiz_id}/attempts",
    status_code=status.HTTP_201_CREATED,
    responses={200: {"description": "An attempt was already in progress and is returned as is"}},
)
def start_attempt(
    quiz_id: str, body: StartAttemptIn, response: Response, session: SessionDep
) -> AttemptOut:
    attempt, created = attempt_service.start_attempt(session, quiz_id, body.guest_ref)
    if not created:
        response.status_code = status.HTTP_200_OK
    return attempt_out(attempt)


@router.get("/attempts/{attempt_id}")
def get_attempt(attempt_id: str, session: SessionDep) -> AttemptOut:
    return attempt_out(attempt_service.get_attempt(session, attempt_id))


@router.put("/attempts/{attempt_id}/answers/{question_id}")
def save_answer(
    attempt_id: str, question_id: str, body: SaveAnswerIn, session: SessionDep
) -> AnswerOut:
    answer = attempt_service.save_answer(session, attempt_id, question_id, body.option_ids)
    return AnswerOut(
        question_id=answer.question_id,
        option_ids=list(answer.selected_option_ids_json),
        answered_at=answer.answered_at,
    )


@router.post("/attempts/{attempt_id}/submit")
def submit_attempt(attempt_id: str, session: SessionDep, settings: SettingsDep) -> AttemptResultOut:
    attempt = attempt_service.submit_attempt(
        session, attempt_id, pass_threshold_pct=settings.pass_threshold_pct
    )
    return attempt_result_out(attempt)


@router.get("/attempts/{attempt_id}/result")
def get_result(attempt_id: str, session: SessionDep) -> AttemptResultOut:
    attempt = attempt_service.get_attempt(session, attempt_id)
    if attempt.status is not AttemptStatus.SUBMITTED:
        raise ConflictError("attempt has not been submitted yet", code="attempt_not_submitted")
    return attempt_result_out(attempt)
