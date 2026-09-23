"""Independent acceptance checks tied to TASK.md, with isolated storage only."""
import time

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.contracts import Card, CardField, FieldName
from app.scoring import score


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_TRACE_DIR", str(tmp_path / "traces"))
    with TestClient(create_app(tmp_path / "requirements.db", "stub")) as value:
        yield value


def new_task(client):
    response = client.post("/api/tasks", json={"text": "Учебному центру нужен помощник", "industry": "Аудит требований"})
    assert response.status_code == 201
    return response.json()["id"]


def publish_low(client, tid):
    assert client.put(f"/api/tasks/{tid}/card", json={"fields": {
        "title": {"value": "Задача аудита", "confirmed": True}
    }}).status_code == 200
    published = client.post(f"/api/tasks/{tid}/publish")
    assert published.status_code == 200
    assert published.json()["score"]["total"] == 0


def proposal(client, tid, team):
    return client.post(f"/api/tasks/{tid}/proposals", json={
        "team_id": team, "idea": "Исследуем вопросы слушателей", "plan": "Составим и проверим прототип",
        "deadline": "Три недели", "link": "https://example.test/prototype"})


def test_business_can_reject_every_team_then_choose_several(client):
    """§2/§3: no team is selected until business decides; rejecting all is valid."""
    tid = new_task(client)
    publish_low(client, tid)
    teams = client.get("/api/teams").json()[:3]
    before = {team["id"]: team["points"] for team in teams}
    proposals = [proposal(client, tid, team["id"]) for team in teams]
    assert all(response.status_code == 201 for response in proposals)
    ids = [response.json()["id"] for response in proposals]
    assert all(response.json()["decision"] == "pending" for response in proposals)
    for pid in ids:
        assert client.post(f"/api/proposals/{pid}/decision", json={"decision": "reject"}).status_code == 200
        assert client.post(f"/api/proposals/{pid}/stage", json={"confirmed": True}).status_code == 409
    assert all(p["decision"] == "reject" for p in client.get(f"/api/tasks/{tid}/proposals").json())
    for pid in ids[:2]:
        assert client.post(f"/api/proposals/{pid}/decision", json={"decision": "accept"}).status_code == 200
    selected = client.get(f"/api/tasks/{tid}/proposals").json()
    assert sum(p["decision"] == "accept" for p in selected) == 2
    assert {t["id"]: t["points"] for t in client.get("/api/teams").json() if t["id"] in before} == before
    for pid in ids[:2]:
        for _ in range(3):
            assert client.post(f"/api/proposals/{pid}/stage", json={"confirmed": True}).status_code == 200
    after = {t["id"]: t["points"] for t in client.get("/api/teams").json()}
    assert [after[t["id"]] - before[t["id"]] for t in teams] == [10, 10, 0]


def test_unconfirmation_unpublishes_and_blocks_new_proposals_until_reconfirmed(client):
    """§4/§5: withdrawing a confirmation must affect score and publication."""
    tid = new_task(client)
    value = "Администраторы вручную отвечают на повторяющиеся вопросы"
    payload = {"fields": {"context": {"value": value, "confirmed": True}}}
    assert client.put(f"/api/tasks/{tid}/card", json=payload).json()["score"]["total"] == 10
    assert client.post(f"/api/tasks/{tid}/publish").status_code == 200
    payload["fields"]["context"]["confirmed"] = False
    result = client.put(f"/api/tasks/{tid}/card", json=payload).json()
    assert result["score"]["total"] == 0
    assert result["status"] == "draft"
    assert tid not in {task["id"] for task in client.get("/api/catalog").json()}
    assert client.post(f"/api/tasks/{tid}/publish").status_code == 409
    assert proposal(client, tid, "team-1").status_code == 409
    payload["fields"]["context"]["confirmed"] = True
    assert client.put(f"/api/tasks/{tid}/card", json=payload).json()["score"]["total"] == 10
    assert client.post(f"/api/tasks/{tid}/publish").status_code == 200
    assert proposal(client, tid, "team-1").status_code == 201


def test_catalog_http_filters_are_reversible_and_do_not_gate_zero_score(client):
    tid = new_task(client)
    publish_low(client, tid)
    all_tasks = client.get("/api/catalog").json()
    scores = [task["score"]["total"] for task in all_tasks]
    assert scores == sorted(scores, reverse=True)
    for level in ("draft", "working", "ready", "priority"):
        response = client.get("/api/catalog", params={"level": level})
        assert response.status_code == 200
        assert {t["id"] for t in response.json()} == {t["id"] for t in all_tasks if t["score"]["level"] == level}
    assert client.get("/api/catalog", params={"topic": "Аудит требований", "level": "priority"}).json() == []
    filtered = client.get("/api/catalog", params={"topic": "Аудит требований", "level": "draft"}).json()
    assert [task["id"] for task in filtered] == [tid]
    assert {task["id"] for task in client.get("/api/catalog").json()} == {task["id"] for task in all_tasks}
    assert proposal(client, tid, "team-1").status_code == 201


@pytest.mark.parametrize("field,value,status", [
    ("team_id", "missing-team", 404), ("idea", "     ", 409),
    ("plan", "     ", 409), ("deadline", " ", 409),
    ("link", "javascript:alert(1)", 409), ("idea", "x" * 10001, 422),
])
def test_invalid_proposal_cannot_create_an_invisible_or_partial_record(client, field, value, status):
    tid = new_task(client)
    publish_low(client, tid)
    body = {"team_id": "team-1", "idea": "Предлагаем решение", "plan": "Проверим прототип",
            "deadline": "Неделя", "link": "https://example.test/demo"}
    body[field] = value
    response = client.post(f"/api/tasks/{tid}/proposals", json=body)
    assert response.status_code == status
    assert client.get(f"/api/tasks/{tid}/proposals").json() == []


def test_no_ai_or_empty_answers_cannot_implicitly_confirm_or_publish(client):
    """§5: a local question path must still require human confirmation."""
    tid = new_task(client)
    assert client.post(f"/api/tasks/{tid}/analyze", json={"mode": "stub"}).status_code == 202
    for _ in range(3):
        for _ in range(100):
            run = client.get(f"/api/tasks/{tid}/run").json()
            if run["status"] in {"waiting_answers", "card_ready", "error"}:
                break
            time.sleep(.01)
        assert run["status"] != "error"
        if run["status"] == "card_ready":
            break
        assert len(run["pending_questions"]) >= 3
        assert client.post(f"/api/tasks/{tid}/publish").status_code == 409
        answers = [{"answer_id": q["answer_id"], "answer": "Не знаю"} for q in run["pending_questions"]]
        assert client.post(f"/api/tasks/{tid}/answers", json={"answers": answers}).status_code == 202
    assert run["status"] == "card_ready"
    task = client.get(f"/api/tasks/{tid}").json()
    assert task["status"] == "draft" and task["score"]["total"] == 0
    assert all(field["status"] != "confirmed" for field in task["card"]["fields"].values())
    assert all(field["value"] != "Не знаю" for field in task["card"]["fields"].values())
    assert client.post(f"/api/tasks/{tid}/publish").status_code == 409


@pytest.mark.parametrize("name,value", [
    ("constraints", "Данные, срок и критерии пока не определены."),
    ("success_criteria", "Данные, срок и критерии пока не определены."),
    ("success_criteria", "Критерии успеха неизвестны."),
])
def test_confirming_explicit_unknown_does_not_earn_readiness_points(name, value):
    """§4: admitting the criterion is unknown does not supply that criterion.

    These exact inputs were returned in live unknown/unknown_expanded followups.
    Human confirmation establishes authorship, not readiness to start work.
    """
    card = Card()
    card.fields[FieldName(name)] = CardField(value=value, status="confirmed")
    assert score(card).total == 0


@pytest.mark.parametrize("name,value,points", [
    ("constraints", "Нельзя передавать персональные данные внешним сервисам.", 10),
    ("constraints", "Срок 4 недели. Не требуется отдельная мобильная версия.", 10),
    ("success_criteria", "Не более 5% тестовых запросов должны завершаться ошибкой.", 15),
])
def test_meaningful_negative_requirements_still_earn_readiness_points(name, value, points):
    """A prohibition or measurable upper bound is useful information, not unknown."""
    card = Card()
    card.fields[FieldName(name)] = CardField(value=value, status="confirmed")
    assert score(card).total == points
