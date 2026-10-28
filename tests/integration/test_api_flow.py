from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pentice_quiz.db.models import LlmGenerationLog, Quiz
from pentice_quiz.llm.client import LLMTimeoutError
from tests.conftest import (
    HARBOR_URL,
    PINE_URL,
    FakeLLMClient,
    as_llm_reply,
    llm_questions,
    load_llm_output,
)

API = "/api/v1"
HARBOR = "harbor_lofts_valid.json"


def _create_quiz(
    client: TestClient, headers: dict[str, str], expected_status: int = 201, **body: Any
) -> dict[str, Any]:
    response = client.post(
        f"{API}/quizzes", json={"building_slug": "harbor-lofts", **body}, headers=headers
    )
    assert response.status_code == expected_status, response.text
    data: dict[str, Any] = response.json()
    return data


def _answer_key(client: TestClient, headers: dict[str, str], quiz_id: str) -> dict[str, Any]:
    response = client.get(f"{API}/admin/quizzes/{quiz_id}", headers=headers)
    assert response.status_code == 200
    return {
        question["id"]: [option["id"] for option in question["options"] if option["is_correct"]]
        for question in response.json()["questions"]
    }


def _start(client: TestClient, quiz_id: str, guest_ref: str = "g-123") -> Any:
    return client.post(f"{API}/quizzes/{quiz_id}/attempts", json={"guest_ref": guest_ref})


def _put_answer(
    client: TestClient, attempt_id: str, question_id: str, option_ids: list[str]
) -> Any:
    return client.put(
        f"{API}/attempts/{attempt_id}/answers/{question_id}", json={"option_ids": option_ids}
    )


@pytest.fixture
def harbor_quiz(
    client: TestClient, fake_llm: FakeLLMClient, admin_headers: dict[str, str]
) -> dict[str, Any]:
    fake_llm.replies.append(load_llm_output(HARBOR))
    return _create_quiz(client, admin_headers)


class TestQuizGeneration:
    def test_harbor_quiz_is_published_without_answer_key(self, harbor_quiz: dict[str, Any]) -> None:
        assert harbor_quiz["generation_mode"] == "llm"
        assert harbor_quiz["is_fallback"] is False
        assert harbor_quiz["reused"] is False
        assert len(harbor_quiz["questions"]) == 6
        assert all(len(q["options"]) == 4 for q in harbor_quiz["questions"])
        assert "is_correct" not in str(harbor_quiz)
        assert "source_quote" not in str(harbor_quiz)

    def test_pine_court_works_without_code_changes(
        self, client: TestClient, fake_llm: FakeLLMClient, admin_headers: dict[str, str]
    ) -> None:
        fake_llm.replies.append(load_llm_output("pine_court_valid.json"))
        quiz = _create_quiz(client, admin_headers, building_slug="pine-court", source_url=PINE_URL)
        assert quiz["building_slug"] == "pine-court"
        assert len(quiz["questions"]) == 6

    def test_admin_view_has_quotes_and_weights(
        self, client: TestClient, harbor_quiz: dict[str, Any], admin_headers: dict[str, str]
    ) -> None:
        admin = client.get(f"{API}/admin/quizzes/{harbor_quiz['id']}", headers=admin_headers)
        body = admin.json()
        assert body["source"]["url"] == HARBOR_URL
        assert [round(q["weight"], 4) for q in body["questions"][:3]] == [1.0, 1.1, 1.21]
        assert body["questions"][0]["source_quote"] == "Check-in opens at **4:00 PM**."

    def test_unchanged_rules_reuse_the_quiz_without_calling_the_llm(
        self,
        client: TestClient,
        fake_llm: FakeLLMClient,
        harbor_quiz: dict[str, Any],
        admin_headers: dict[str, str],
    ) -> None:
        calls_before = len(fake_llm.calls)
        again = _create_quiz(client, admin_headers, expected_status=200)
        assert again["id"] == harbor_quiz["id"]
        assert again["reused"] is True
        assert len(fake_llm.calls) == calls_before

    def test_changed_rules_create_a_new_quiz(
        self,
        client: TestClient,
        fake_llm: FakeLLMClient,
        harbor_quiz: dict[str, Any],
        admin_headers: dict[str, str],
        source_documents: dict[str, str],
    ) -> None:
        source_documents[HARBOR_URL] += "\n- Bikes go in the rack on level B1.\n"
        fake_llm.replies.append(load_llm_output(HARBOR))
        new_quiz = _create_quiz(client, admin_headers)
        assert new_quiz["id"] != harbor_quiz["id"]
        old = client.get(f"{API}/quizzes/{harbor_quiz['id']}")
        assert old.json()["questions"] == harbor_quiz["questions"]

    def test_requires_api_key(self, client: TestClient) -> None:
        response = client.post(f"{API}/quizzes", json={"building_slug": "harbor-lofts"})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"

    def test_invalid_body_uses_error_format(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = client.post(
            f"{API}/quizzes", json={"building_slug": "Harbor Lofts!"}, headers=admin_headers
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"

    def test_unknown_quiz_is_404(self, client: TestClient) -> None:
        response = client.get(f"{API}/quizzes/nope")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "quiz_not_found"


class TestLlmFailures:
    def test_malformed_output_falls_back_to_template(
        self,
        client: TestClient,
        fake_llm: FakeLLMClient,
        admin_headers: dict[str, str],
        session: Session,
    ) -> None:
        fake_llm.replies.extend(
            [load_llm_output("truncated.json"), "I cannot help with that", '{"questions": 5}']
        )
        quiz = _create_quiz(client, admin_headers)
        assert quiz["generation_mode"] == "fallback_template"
        assert quiz["is_fallback"] is True
        assert 5 <= len(quiz["questions"]) <= 8
        logs = session.scalars(select(LlmGenerationLog)).all()
        assert [log.status for log in logs] == ["invalid", "invalid", "invalid"]
        assert all(log.quiz_id == quiz["id"] for log in logs)
        assert "not valid quiz JSON" in logs[0].validation_errors_json[0]

    def test_timeouts_with_cached_quiz_reuse_it(
        self,
        client: TestClient,
        fake_llm: FakeLLMClient,
        harbor_quiz: dict[str, Any],
        admin_headers: dict[str, str],
    ) -> None:
        fake_llm.replies.extend([LLMTimeoutError("slow")] * 3)
        quiz = _create_quiz(client, admin_headers, force=True)
        assert quiz["generation_mode"] == "fallback_cache"
        assert quiz["id"] != harbor_quiz["id"]
        assert [q["text"] for q in quiz["questions"]] == [
            q["text"] for q in harbor_quiz["questions"]
        ]

    def test_nothing_works_returns_503_and_publishes_nothing(
        self,
        client: TestClient,
        fake_llm: FakeLLMClient,
        admin_headers: dict[str, str],
        source_documents: dict[str, str],
        session: Session,
    ) -> None:
        source_documents[HARBOR_URL] = "# Plain\n## Rules\nBe kind. No parties.\n"
        fake_llm.replies.extend([LLMTimeoutError("slow")] * 3)
        response = client.post(
            f"{API}/quizzes", json={"building_slug": "harbor-lofts"}, headers=admin_headers
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "generation_unavailable"
        assert "Traceback" not in response.text
        assert session.scalar(select(func.count()).select_from(Quiz)) == 0

    def test_source_down_is_502(self, client: TestClient, admin_headers: dict[str, str]) -> None:
        response = client.post(
            f"{API}/quizzes",
            json={"building_slug": "x", "source_url": "https://rules.example.com/missing.md"},
            headers=admin_headers,
        )
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "source_unavailable"

    def test_partially_valid_output_is_repaired(
        self, client: TestClient, fake_llm: FakeLLMClient, admin_headers: dict[str, str]
    ) -> None:
        questions = llm_questions(HARBOR)
        fake_llm.replies.extend([as_llm_reply(questions[:3]), as_llm_reply(questions[3:6])])
        quiz = _create_quiz(client, admin_headers)
        assert quiz["generation_mode"] == "llm"
        assert len(quiz["questions"]) == 6


class TestAttemptLifecycle:
    def test_full_flow_scores_and_reports(
        self, client: TestClient, harbor_quiz: dict[str, Any], admin_headers: dict[str, str]
    ) -> None:
        quiz_id = harbor_quiz["id"]
        key = _answer_key(client, admin_headers, quiz_id)
        started = _start(client, quiz_id)
        assert started.status_code == 201
        attempt = started.json()
        question_ids = [q["id"] for q in attempt["questions"]]

        # Q1 right, Q2 (multi) only one of two correct, Q3 wrong, rest right.
        _put_answer(client, attempt["id"], question_ids[0], key[question_ids[0]])
        _put_answer(client, attempt["id"], question_ids[1], key[question_ids[1]][:1])
        wrong = next(
            o["id"]
            for o in attempt["questions"][2]["options"]
            if o["id"] not in key[question_ids[2]]
        )
        _put_answer(client, attempt["id"], question_ids[2], [wrong])
        for question_id in question_ids[3:]:
            assert (
                _put_answer(client, attempt["id"], question_id, key[question_id]).status_code == 200
            )

        result = client.post(f"{API}/attempts/{attempt['id']}/submit").json()
        scores = [q["score"] for q in result["questions"]]
        assert scores == [4.0, 2.0, 0.0, 4.0, 4.0, 4.0]
        weights = [1.1**i for i in range(6)]
        expected = sum(w * s for w, s in zip(weights, scores, strict=True)) / sum(weights)
        assert result["final_score"] == round(expected, 2)
        assert result["final_pct"] == round(expected / 4 * 100, 2)
        assert result["final_pct"] == 77.19
        assert result["passed"] is True
        assert result["questions"][2]["explanation"]

        missed = client.get(
            f"{API}/ops/quizzes/{quiz_id}/missed-questions?min_attempts=1", headers=admin_headers
        ).json()
        # Both missed once (rate 1.0); ties are ordered by position.
        assert [m["question_id"] for m in missed[:2]] == [question_ids[1], question_ids[2]]
        assert missed[0]["miss_rate"] == 1.0
        wrong_stat = next(m for m in missed if m["question_id"] == question_ids[2])
        assert wrong_stat["most_chosen_wrong_option"]["option_id"] == wrong

        guest = client.get(f"{API}/ops/guests/g-123", headers=admin_headers).json()
        assert guest["latest"]["attempt_id"] == attempt["id"]
        assert guest["latest_submitted"]["final_pct"] == result["final_pct"]
        assert guest["latest_submitted"]["missed_topics"] == ["check_in", "visitors"]

        topics = client.get(
            f"{API}/ops/buildings/harbor-lofts/missed-topics?min_attempts=1",
            headers=admin_headers,
        ).json()
        assert {t["topic"] for t in topics if t["misses"]} == {"check_in", "visitors"}

    def test_reload_returns_same_attempt_with_saved_answers(
        self, client: TestClient, harbor_quiz: dict[str, Any]
    ) -> None:
        attempt = _start(client, harbor_quiz["id"]).json()
        question = attempt["questions"][0]
        _put_answer(client, attempt["id"], question["id"], [question["options"][0]["id"]])

        reloaded = _start(client, harbor_quiz["id"])
        assert reloaded.status_code == 200
        assert reloaded.json()["id"] == attempt["id"]
        assert reloaded.json()["answers"] == {question["id"]: [question["options"][0]["id"]]}
        state = client.get(f"{API}/attempts/{attempt['id']}").json()
        assert state["answers"] == reloaded.json()["answers"]

    def test_answer_can_change_before_submit(
        self, client: TestClient, harbor_quiz: dict[str, Any]
    ) -> None:
        attempt = _start(client, harbor_quiz["id"]).json()
        question = attempt["questions"][0]
        _put_answer(client, attempt["id"], question["id"], [question["options"][0]["id"]])
        _put_answer(client, attempt["id"], question["id"], [question["options"][1]["id"]])
        state = client.get(f"{API}/attempts/{attempt['id']}").json()
        assert state["answers"][question["id"]] == [question["options"][1]["id"]]

    def test_double_submit_is_idempotent_and_answers_freeze(
        self, client: TestClient, harbor_quiz: dict[str, Any]
    ) -> None:
        attempt = _start(client, harbor_quiz["id"]).json()
        first = client.post(f"{API}/attempts/{attempt['id']}/submit")
        second = client.post(f"{API}/attempts/{attempt['id']}/submit")
        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()
        assert first.json()["final_score"] == 0.0
        assert first.json()["passed"] is False

        question = attempt["questions"][0]
        late = _put_answer(client, attempt["id"], question["id"], [question["options"][0]["id"]])
        assert late.status_code == 409
        assert late.json()["error"]["code"] == "attempt_submitted"

    def test_retake_creates_a_new_linked_attempt(
        self, client: TestClient, harbor_quiz: dict[str, Any], admin_headers: dict[str, str]
    ) -> None:
        first = _start(client, harbor_quiz["id"]).json()
        client.post(f"{API}/attempts/{first['id']}/submit")
        retake = _start(client, harbor_quiz["id"])
        assert retake.status_code == 201
        assert retake.json()["attempt_number"] == 2
        assert retake.json()["previous_attempt_id"] == first["id"]
        assert retake.json()["answers"] == {}

        guest = client.get(f"{API}/ops/guests/g-123", headers=admin_headers).json()
        assert guest["latest"]["attempt_id"] == retake.json()["id"]
        assert guest["latest_submitted"]["attempt_id"] == first["id"]
        assert len(guest["history"]) == 2

    def test_single_select_rejects_two_options(
        self, client: TestClient, harbor_quiz: dict[str, Any]
    ) -> None:
        attempt = _start(client, harbor_quiz["id"]).json()
        question = attempt["questions"][0]
        assert question["multi_select"] is False
        option_ids = [o["id"] for o in question["options"][:2]]
        response = _put_answer(client, attempt["id"], question["id"], option_ids)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "single_select_question"

    def test_option_from_another_question_is_rejected(
        self, client: TestClient, harbor_quiz: dict[str, Any]
    ) -> None:
        attempt = _start(client, harbor_quiz["id"]).json()
        other_option = attempt["questions"][1]["options"][0]["id"]
        response = _put_answer(client, attempt["id"], attempt["questions"][0]["id"], [other_option])
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "unknown_option"

    def test_result_before_submit_is_409(
        self, client: TestClient, harbor_quiz: dict[str, Any]
    ) -> None:
        attempt = _start(client, harbor_quiz["id"]).json()
        response = client.get(f"{API}/attempts/{attempt['id']}/result")
        assert response.status_code == 409

    def test_ops_routes_require_api_key(self, client: TestClient) -> None:
        assert client.get(f"{API}/ops/guests/g-123").status_code == 401


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.json() == {"status": "ok", "database": "ok"}
    assert response.headers["X-Request-ID"]
