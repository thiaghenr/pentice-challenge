import pytest
from sqlalchemy.orm import Session

from pentice_quiz.db.models import Building, Option, Question, Quiz, SourceSnapshot
from pentice_quiz.domain.schemas import GenerationMode, QuizStatus, Strategy, Topic
from pentice_quiz.errors import ImmutableRecordError


@pytest.fixture
def question(session: Session) -> Question:
    building = Building(slug="b", name="B")
    session.add(building)
    session.flush()
    snapshot = SourceSnapshot(building_id=building.id, url="u", content="c", content_hash="h")
    session.add(snapshot)
    session.flush()
    quiz = Quiz(
        building_id=building.id,
        source_snapshot_id=snapshot.id,
        status=QuizStatus.PUBLISHED,
        strategy=Strategy.BY_SECTION,
        generation_mode=GenerationMode.LLM,
    )
    created = Question(
        position=1,
        text="Question?",
        topic=Topic.NOISE,
        section="Noise",
        source_quote="quote",
        explanation="why",
        multi_select=False,
        weight=1.0,
        options=[Option(position=1, text="A", is_correct=True)],
    )
    quiz.questions.append(created)
    session.add(quiz)
    session.commit()
    return created


def test_question_cannot_be_updated(session: Session, question: Question) -> None:
    question.text = "Edited?"
    with pytest.raises(ImmutableRecordError):
        session.commit()


def test_option_cannot_be_updated(session: Session, question: Question) -> None:
    question.options[0].is_correct = False
    with pytest.raises(ImmutableRecordError):
        session.commit()
