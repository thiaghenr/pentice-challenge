from fastapi import APIRouter, Depends, Response, status

from pentice_quiz.api.deps import (
    HttpClientDep,
    QuizAgentDep,
    SessionDep,
    SettingsDep,
    require_api_key,
)
from pentice_quiz.api.schemas import (
    CreateQuizIn,
    QuizAdminOut,
    QuizCreatedOut,
    QuizOut,
    quiz_admin_out,
    quiz_out,
)
from pentice_quiz.db.models import Quiz
from pentice_quiz.domain.schemas import QuizStatus
from pentice_quiz.errors import NotFoundError
from pentice_quiz.services.generation import QuizRequest, create_quiz

router = APIRouter(tags=["quizzes"])


@router.post(
    "/quizzes",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key)],
    responses={200: {"description": "Rules unchanged: the existing quiz was reused"}},
)
def create_quiz_endpoint(
    body: CreateQuizIn,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    http_client: HttpClientDep,
    agent: QuizAgentDep,
) -> QuizCreatedOut:
    creation = create_quiz(
        session,
        QuizRequest(
            building_slug=body.building_slug,
            source_url=str(body.source_url) if body.source_url else None,
            strategy=body.strategy,
            num_questions=body.num_questions,
            force=body.force,
        ),
        settings=settings,
        http_client=http_client,
        agent=agent,
    )
    if creation.reused:
        response.status_code = status.HTTP_200_OK
    return QuizCreatedOut(**quiz_out(creation.quiz).model_dump(), reused=creation.reused)


@router.get("/quizzes/{quiz_id}")
def get_quiz(quiz_id: str, session: SessionDep) -> QuizOut:
    return quiz_out(_get_published_quiz(session, quiz_id))


@router.get("/admin/quizzes/{quiz_id}", dependencies=[Depends(require_api_key)])
def get_quiz_admin(quiz_id: str, session: SessionDep) -> QuizAdminOut:
    quiz = session.get(Quiz, quiz_id)
    if quiz is None:
        raise NotFoundError(f"quiz {quiz_id} not found", code="quiz_not_found")
    return quiz_admin_out(quiz)


def _get_published_quiz(session: SessionDep, quiz_id: str) -> Quiz:
    quiz = session.get(Quiz, quiz_id)
    if quiz is None or quiz.status is not QuizStatus.PUBLISHED:
        raise NotFoundError(f"quiz {quiz_id} not found", code="quiz_not_found")
    return quiz
