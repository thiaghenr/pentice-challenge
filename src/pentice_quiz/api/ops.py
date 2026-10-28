from typing import Annotated

from fastapi import APIRouter, Depends, Query

from pentice_quiz.api.deps import SessionDep, SettingsDep, require_api_key
from pentice_quiz.api.schemas import (
    GuestSummaryOut,
    MissedQuestionOut,
    MissedTopicOut,
    attempt_summary_out,
    missed_question_out,
    missed_topic_out,
)
from pentice_quiz.domain.schemas import AttemptStatus
from pentice_quiz.services import analytics
from pentice_quiz.services.attempts import list_guest_attempts

router = APIRouter(prefix="/ops", tags=["operations"], dependencies=[Depends(require_api_key)])

MinAttempts = Annotated[
    int | None,
    Query(ge=1, description="Hide questions/topics with fewer submitted answers (noise filter)"),
]


@router.get("/guests/{guest_ref}")
def guest_summary(guest_ref: str, session: SessionDep) -> GuestSummaryOut:
    history = [attempt_summary_out(attempt) for attempt in list_guest_attempts(session, guest_ref)]
    latest_submitted = next(
        (summary for summary in history if summary.status is AttemptStatus.SUBMITTED), None
    )
    return GuestSummaryOut(
        guest_ref=guest_ref, latest=history[0], latest_submitted=latest_submitted, history=history
    )


@router.get("/quizzes/{quiz_id}/missed-questions")
def missed_questions(
    quiz_id: str, session: SessionDep, settings: SettingsDep, min_attempts: MinAttempts = None
) -> list[MissedQuestionOut]:
    stats = analytics.missed_questions(
        session, quiz_id, min_attempts=min_attempts or settings.missed_min_attempts
    )
    return [missed_question_out(stat) for stat in stats]


@router.get("/buildings/{slug}/missed-topics")
def missed_topics(
    slug: str, session: SessionDep, settings: SettingsDep, min_attempts: MinAttempts = None
) -> list[MissedTopicOut]:
    stats = analytics.missed_topics(
        session, slug, min_attempts=min_attempts or settings.missed_min_attempts
    )
    return [missed_topic_out(stat) for stat in stats]
