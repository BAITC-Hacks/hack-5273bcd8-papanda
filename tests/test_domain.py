from concurrent.futures import ThreadPoolExecutor
import time
import pytest
from fastapi.testclient import TestClient
from app.api import create_app
from app.contracts import Card, CardField, FieldName, Team, ProposalCreate
from app.scoring import score, field_points
from app.store import Store


VALUES = {
    "title": "Автоматизация обращений",
    "context": "Сотрудники вручную обрабатывают обращения клиентов",
    "need": "Нужно сократить время обработки входящих обращений",
    "users": "Решение используют операторы службы поддержки",
    "data": "Доступна CSV выгрузка обращений за последний год",
    "constraints": "Срок реализации не более 4 недель на Python",
    "expected_result": "Работающий веб прототип для обработки обращений",
    "success_criteria": "Время обработки не более 5 минут на обращение",
    "contact": "owner@example.com",
    "interaction_format": "Еженедельная консультация и обратная связь по почте",
}


def full_card(status="confirmed"):
    return Card(fields={FieldName(k): CardField(value=v, status=status) for k, v in VALUES.items()})


def test_scoring_confirmations_and_rules():
    assert score(full_card()).total == 100
    assert score(full_card("ai_proposed")).total == 0
    card = full_card()
    card.fields[FieldName.success_criteria].value = "Результат должен понравиться всем пользователям"
    assert score(card).total == 92.5
    card.fields[FieldName.data].value = "Данных пока нет, их предстоит собирать отдельно"
    assert score(card).total == 72.5
    card.fields[FieldName.constraints].value = "Мы ожидаем качественную и приятную работу"
    assert score(card).total == 62.5
    card.fields[FieldName.contact].value = "Позвоните нашему руководителю"
    assert score(card).total == 57.5
    gains = [m.max_gain for m in score(card).missing]
    assert gains == sorted(gains, reverse=True)


@pytest.mark.parametrize("value,points", [("", 0), ("нет", 0), ("a"*19, 0), ("a"*20, 10)])
def test_min_length(value, points):
    assert field_points(FieldName.context, value, True)[0] == points


@pytest.mark.parametrize("value,valid", [("a@b.kz", True), ("@business", True), ("+7 (777) 123-45-67", True), ("Ivan", False)])
def test_contact(value, valid):
    assert field_points(FieldName.contact, value, True)[0] == (5 if valid else 0)


@pytest.mark.parametrize("value", [
    "Данные отсутствуют, CSV пока недоступен",
    "Данные будут собраны позднее в таблицу",
    "Нет доступа к CSV файлу с историей заявок",
    "CSV выгрузка пока не доступна студентам",
    "Планируем собрать данные в таблицу после старта",
    "Будем собирать историю обращений в CSV",
    "Предстоит получить отчёты о продажах магазина",
    "Часть данных недоступна, доступен CSV с примерами",
])
def test_unavailable_future_and_ambiguous_materials_do_not_score(value):
    points, reason = field_points(FieldName.data, value, True)
    assert points == 0
    assert "уже доступный" in reason


@pytest.mark.parametrize("value", [
    "Доступна CSV выгрузка истории заявок клиентов",
    "Есть таблица продаж и примеры чеков за месяц",
    "Передадим готовый CSV файл истории обращений",
])
def test_available_materials_score(value):
    assert field_points(FieldName.data, value, True)[0] == 20


@pytest.mark.parametrize("name,value", [
    (FieldName.constraints, "Данные, срок и критерии пока не определены."),
    (FieldName.success_criteria, "Данные, срок и критерии пока не определены."),
    (FieldName.success_criteria, "Критерии успеха неизвестны."),
    (FieldName.success_criteria, "Критерии приемки пока не установлены."),
    (FieldName.success_criteria, "Пока неизвестны критерии успешности решения."),
    (FieldName.constraints, "Срок выполнения пока не определён."),
    (FieldName.constraints, "Ограничения проекта пока неизвестны."),
    (FieldName.constraints, "Бюджет на разработку не установлен."),
])
def test_explicitly_unknown_limits_and_criteria_do_not_score(name, value):
    assert field_points(name, value, True)[0] == 0


@pytest.mark.parametrize("name,value,points", [
    (FieldName.success_criteria, "Время ответа не более 5 секунд", 15),
    (FieldName.constraints, "Нельзя передавать персональные данные третьим лицам", 10),
    (FieldName.constraints, "Не передавать персональные данные через внешний API", 10),
    (FieldName.constraints, "Не передавать персональные данные", 10),
    (FieldName.data, "Доступен CSV с данными неизвестных пользователей", 20),
    (FieldName.success_criteria, "Обработать 30 запросов от неизвестных пользователей", 15),
    (FieldName.constraints, "Критерии неизвестны. Срок разработки — 4 недели.", 10),
    (FieldName.success_criteria, "Срок неизвестен. Критерии: 80% верных ответов.", 15),
])
def test_unknown_detection_preserves_real_bounds_and_other_fields(name, value, points):
    assert field_points(name, value, True)[0] == points


@pytest.mark.parametrize("names,level,total", [([], "draft", 0), (["data", "context", "need"], "working", 40), (["data", "context", "need", "expected_result", "success_criteria"], "ready", 70), (["data", "context", "need", "expected_result", "success_criteria", "users", "constraints"], "priority", 90)])
def test_level_boundaries(names, level, total):
    card = full_card("edited")
    for name in names:
        card.fields[FieldName(name)].status = "confirmed"
    result = score(card)
    assert (result.total, result.level) == (total, level)


def wait_for(client, task_id, statuses):
    for _ in range(100):
        state = client.get(f"/api/tasks/{task_id}/run").json()
        if state["status"] in statuses:
            return state
        time.sleep(.01)
    pytest.fail(f"AI did not reach {statuses}: {state}")


def test_end_to_end_stub_and_manual_provenance(tmp_path):
    app = create_app(tmp_path / "nested" / "test.db", "stub")
    with TestClient(app) as client:
        app.state.store.save_team(Team(id="team", name="Test"))
        task = client.post("/api/tasks", json={"text": "Нам нужен бот для поддержки клиентов", "industry": "Образование"}).json()
        tid = task["id"]
        assert client.post(f"/api/tasks/{tid}/publish").status_code == 409
        assert client.post(f"/api/tasks/{tid}/analyze", json={}).status_code == 202
        state = wait_for(client, tid, {"waiting_answers"})
        assert len(state["pending_questions"]) >= 3
        assert client.put(f"/api/tasks/{tid}/card", json={"fields": {}}).status_code == 409
        for _ in range(2):
            answers = [{"answer_id": q["answer_id"], "answer": VALUES[q["field"]]} for q in state["pending_questions"]]
            assert client.post(f"/api/tasks/{tid}/answers", json={"answers": answers}).status_code == 202
            state = wait_for(client, tid, {"waiting_answers", "card_ready", "error"})
            if state["status"] == "card_ready": break
        assert state["status"] == "card_ready"
        assert client.post(f"/api/tasks/{tid}/publish").status_code == 409
        result = client.put(f"/api/tasks/{tid}/card", json={"fields": {k: {"value": v, "confirmed": True} for k, v in VALUES.items()}}).json()
        assert result["score"]["total"] == 100
        contact_source = result["card"]["fields"]["contact"]["sources"][0]
        assert result["sources"][contact_source["source_id"]] == VALUES["contact"]
        revision = result["revision"]
        assert client.post(f"/api/tasks/{tid}/publish").status_code == 200
        assert tid in {item["id"] for item in client.get("/api/catalog?level=priority&topic=Образ").json()}
        result = client.put(f"/api/tasks/{tid}/card", json={"fields": {"need": {"value": "Изменённая потребность бизнеса для студентов", "confirmed": False}}}).json()
        assert result["status"] == "draft" and result["revision"] == revision + 1
        assert result["score"]["total"] == 90
        assert tid not in {item["id"] for item in client.get("/api/catalog").json()}
        assert client.get("/api/does-not-exist").status_code == 404
    reopened = Store(str(tmp_path / "nested" / "test.db"))
    assert reopened.get_task(tid).revision == revision + 1
    reopened.close()


def test_low_score_unlimited_proposals_multiple_choices_and_stage(tmp_path):
    app = create_app(tmp_path / "test.db", "stub")
    with TestClient(app) as client:
        for team in ("one", "two"):
            app.state.store.save_team(Team(id=team, name=team))
        tid = client.post("/api/tasks", json={"text": "Короткая задача", "industry": "Образование"}).json()["id"]
        client.put(f"/api/tasks/{tid}/card", json={"fields": {"title": {"value": "Маленькая задача", "confirmed": True}}})
        assert client.post(f"/api/tasks/{tid}/publish").json()["score"]["total"] == 0
        assert tid in {item["id"] for item in client.get("/api/catalog?level=draft").json()}
        ids = []
        for team in ("one", "one", "two"):
            response = client.post(f"/api/tasks/{tid}/proposals", json={"team_id": team, "idea": "Сделаем прототип", "plan": "Исследуем и проверим", "deadline": "2 недели", "link": ""})
            assert response.status_code == 201
            ids.append(response.json()["id"])
        assert client.post(f"/api/proposals/{ids[0]}/stage", json={"confirmed": True}).status_code == 409
        for pid in (ids[0], ids[2]):
            assert client.post(f"/api/proposals/{pid}/decision", json={"decision": "accept"}).status_code == 200
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(app.state.store.confirm_stage, [ids[0]] * 8))
        assert all(p.stage_confirmed for p in results)
        assert app.state.store.get_team("one").points == 10
        assert client.post(f"/api/proposals/{ids[0]}/decision", json={"decision": "reject"}).status_code == 409
        assert len(client.get(f"/api/tasks/{tid}/proposals").json()) == 3


def test_stage_atomic_across_database_connections(tmp_path):
    path = str(tmp_path / "atomic.db")
    store = Store(path)
    task = store.create_task("Опубликованная задача", "Образование")
    task.status = "published"
    store.save_task(task)
    store.save_team(Team(id="team", name="Команда"))
    proposal = store.create_proposal(task.id, ProposalCreate(team_id="team", idea="Идея решения", plan="План решения", deadline="2 недели", link=""))
    proposal.decision = "accept"
    store.save_proposal(proposal)

    def confirm(_):
        connection = Store(path)
        try:
            return connection.confirm_stage(proposal.id)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(item.stage_confirmed for item in pool.map(confirm, range(8)))
    assert store.get_team("team").points == 10
    store.close()


def test_catalog_sorting_and_filters():
    from app.catalog import catalog
    from app.contracts import Task
    def task(id, total, published, industry="Education", status="published"):
        t = Task(id=id, text="Description", industry=industry, created_at=published, published_at=published, status=status)
        t.score.total = total
        return t
    tasks = [task("low", 0, "2026-09-23"), task("old", 70, "2026-09-22"), task("new", 70, "2026-09-23"), task("hidden", 100, "2026-09-23", status="draft"), task("health", 20, "2026-09-23", industry="Health")]
    assert [t.id for t in catalog(tasks)] == ["new", "old", "health", "low"]
    assert [t.id for t in catalog(tasks, "educ")] == ["new", "old", "low"]


def test_real_seed_integrity_and_idempotence(tmp_path):
    store = Store(str(tmp_path / "seed.db"))
    try:
        store.seed()
        tasks = store.list_tasks()
        teams = store.list_teams()
        published = [t for t in tasks if t.status == "published"]
        assert len([t for t in tasks if t.status == "draft"]) >= 5
        assert len(published) == 5
        assert len(teams) >= 5
        assert sorted(t.score.total for t in published) == [20, 50, 75, 100, 100]
        team_ids = {t.id for t in teams}
        proposals = [p for task in tasks for p in store.list_proposals(task.id)]
        assert len(proposals) >= 5
        assert all(p.team_id in team_ids for p in proposals)
        for task in tasks:
            assert task.sources.get("draft") == task.text
            for field in task.card.fields.values():
                if not field.value:
                    continue
                assert field.sources
                for source in field.sources:
                    assert source.source_id in task.sources
                    assert source.quote in task.sources[source.source_id]
        edited = teams[0]
        edited.points += 10
        store.save_team(edited)
        store.seed()
        assert len(store.list_tasks()) == len(tasks)
        assert len(store.list_teams()) == len(teams)
        assert sum(len(store.list_proposals(t.id)) for t in tasks) == len(proposals)
        assert store.get_team(edited.id).points == edited.points
    finally:
        store.close()


def test_database_environment_precedence(tmp_path, monkeypatch):
    primary, legacy = tmp_path / "primary.db", tmp_path / "legacy.db"
    monkeypatch.setenv("SANA_DB", str(primary))
    monkeypatch.setenv("DB_PATH", str(legacy))
    with TestClient(create_app(ai_mode="stub")) as client:
        assert client.get("/api/health").status_code == 200
    assert primary.exists()
    assert not legacy.exists()


def test_store_closed_after_application_shutdown(tmp_path):
    import sqlite3
    app = create_app(tmp_path / "closed.db", "stub")
    with TestClient(app) as client:
        assert client.get("/api/tasks").status_code == 200
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        app.state.store.list_tasks()
