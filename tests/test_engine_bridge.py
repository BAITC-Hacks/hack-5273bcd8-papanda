"""The new engine reaches the unchanged product card-confirmation boundary."""
import asyncio
import json
from unittest.mock import patch

import httpx
import pytest

from app.ai.service import TaskRunService
from app.api import create_app
from app.contracts import Answer as ProductAnswer, Task
from app.contracts import CardField, FieldName, Source
from engines.common.llm import ScriptedLLM
from engines.light import LightEngine
from test_light_engine import DRAFT, analysis_fixture, card_fixture


class MemoryStore:
    def __init__(self):
        self.task = Task(id="t", text=DRAFT, industry="test", created_at="now")

    def get_task(self, task_id):
        if task_id != "t":
            raise KeyError(task_id)
        return self.task.model_copy(deep=True)

    def save_task(self, task):
        self.task = task.model_copy(deep=True)


async def wait_for(service, status):
    for _ in range(1000):
        state = service.get("t")
        if state.status == status:
            return state
        if state.status == "error":
            raise AssertionError(state.model_dump())
        await asyncio.sleep(0.001)
    raise AssertionError("Engine bridge did not advance")


@pytest.mark.asyncio
async def test_light_engine_to_product_unconfirmed_card():
    engine = LightEngine(
        actor=ScriptedLLM([json.dumps(analysis_fixture(), ensure_ascii=False),
                           json.dumps(card_fixture(), ensure_ascii=False)]),
        judge=ScriptedLLM(['{"accepted":true,"issues":[]}']), max_rounds=1,
    )
    service = TaskRunService(MemoryStore())
    with patch("engines.get_engine", return_value=engine):
        try:
            state = service.start("t", "light")
            assert state.status == "analyzing"
            waiting = await wait_for(service, "waiting_answers")
            assert len(waiting.pending_questions) >= 3
            await service.submit_answers("t", [ProductAnswer(
                answer_id=q.answer_id, answer="Доступен CSV" if q.field.value == "data" else "Не знаю")
                for q in waiting.pending_questions])
            ready = await wait_for(service, "card_ready")
            assert ready.card_draft.fields["data"].value == "Доступен CSV"
            assert ready.card_draft.fields["data"].status == "ai_proposed"
            assert service.store.task.card.fields["data"].status == "ai_proposed"
        finally:
            await service.close()


@pytest.mark.asyncio
async def test_light_engine_through_product_http_api(tmp_path):
    engine = LightEngine(
        actor=ScriptedLLM([json.dumps(analysis_fixture(), ensure_ascii=False),
                           json.dumps(card_fixture(), ensure_ascii=False)]),
        judge=ScriptedLLM(['{"accepted":true,"issues":[]}']), max_rounds=1,
    )
    app = create_app(db_path=tmp_path / "light.sqlite3", ai_mode="stub")
    with patch("engines.get_engine", return_value=engine):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            task = (await client.post("/api/tasks", json={"text": DRAFT, "industry": "services"})).json()
            task_id = task["id"]
            started = await client.post(f"/api/tasks/{task_id}/analyze", json={"mode": "light"})
            assert started.status_code == 202
            for _ in range(1000):
                run = (await client.get(f"/api/tasks/{task_id}/run")).json()
                if run["status"] == "waiting_answers":
                    break
                await asyncio.sleep(0.001)
            assert run["status"] == "waiting_answers"
            answer_payload = {"answers": [
                {"answer_id": q["answer_id"], "answer": "Доступен CSV" if q["field"] == "data" else "Не знаю"}
                for q in run["pending_questions"]]}
            submitted = await client.post(f"/api/tasks/{task_id}/answers", json=answer_payload)
            assert submitted.status_code == 202
            # The UI polls only active states; accepting answers must leave waiting
            # before the background engine task gets another event-loop turn.
            assert submitted.json()["status"] == "building_card"
            assert submitted.json()["pending_questions"] == []
            duplicate = await client.post(f"/api/tasks/{task_id}/answers", json=answer_payload)
            assert duplicate.status_code == 409
            for _ in range(1000):
                run = (await client.get(f"/api/tasks/{task_id}/run")).json()
                if run["status"] == "card_ready":
                    break
                await asyncio.sleep(0.001)
            assert run["status"] == "card_ready"
            persisted = (await client.get(f"/api/tasks/{task_id}")).json()
            assert persisted["card"]["fields"]["data"]["value"] == "Доступен CSV"
            assert persisted["card"]["fields"]["data"]["status"] == "ai_proposed"
            # «Не знаю» never becomes a card value.
            assert all(f["value"] != "Не знаю" for f in persisted["card"]["fields"].values())
        await app.state.service.close()
        app.state.store.close()


@pytest.mark.asyncio
async def test_reanalysis_uses_current_manual_fields_and_prior_user_answers_without_contact(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_TRACE_DIR", str(tmp_path / "traces"))
    store = MemoryStore()
    manual = "Срок прототипа изменён: две недели на Python."
    previous_answer = "Нужен веб-прототип для ответов на вопросы клиентов."
    store.task.sources = {"draft": DRAFT, "A1": previous_answer,
                          "prior:earlier:A1": "Ранее проведено интервью с администратором.",
                          "edit_current": manual, "edit_contact": "owner@example.test",
                          "edit_old_contact": "old@example.test"}
    store.task.card.fields[FieldName.constraints] = CardField(
        value=manual, status="confirmed", sources=[Source(source_id="edit_current", quote=manual)])
    store.task.card.fields[FieldName.contact] = CardField(
        value="owner@example.test", status="confirmed",
        sources=[Source(source_id="edit_contact", quote="owner@example.test")])
    store.task.card.fields[FieldName.users] = CardField(value="Неподтверждённая интерпретация модели", status="ai_proposed")
    analysis = analysis_fixture()
    analysis["simplest"]["elements"][2]["quote"] = manual
    result = card_fixture()
    result["card"]["fields"]["expected_result"] = {
        "value": previous_answer, "sources": [{"source_id": "prior:0:A1", "quote": previous_answer}]}
    actor = ScriptedLLM([json.dumps(analysis, ensure_ascii=False), json.dumps(result, ensure_ascii=False)])
    engine = LightEngine(actor=actor, judge=ScriptedLLM(['{"accepted":true,"issues":[]}']), max_rounds=1)
    service = TaskRunService(store)
    with patch("engines.get_engine", return_value=engine):
        try:
            service.start("t", "light")
            waiting = await wait_for(service, "waiting_answers")
            payload = json.loads(actor.calls[0]["messages"][-1]["content"])
            assert payload["draft"] == DRAFT
            assert payload["sources"]["field:constraints"] == manual
            assert payload["sources"]["prior:0:A1"] == previous_answer
            assert payload["sources"]["prior:earlier:A1"] == "Ранее проведено интервью с администратором."
            assert not any(key.startswith("prior:0:prior:") for key in payload["sources"])
            assert "A1" not in payload["sources"]
            assert "field:contact" not in payload["sources"]
            serialized = json.dumps(actor.calls, ensure_ascii=False)
            assert "owner@example.test" not in serialized and "old@example.test" not in serialized
            assert "Неподтверждённая интерпретация модели" not in serialized
            await service.submit_answers("t", [ProductAnswer(answer_id=q.answer_id,
                answer="Доступен CSV" if q.field.value == "data" else "Не знаю") for q in waiting.pending_questions])
            ready = await wait_for(service, "card_ready")
            assert ready.card_draft.fields["expected_result"].value == previous_answer
            assert ready.card_draft.fields["constraints"].value == manual
            sources = service.contexts["t"]["sources"]
            assert sources["prior:0:A1"] == previous_answer
            assert sources["A1"] == "Доступен CSV"
            assert ready.card_draft.fields["expected_result"].sources[0].source_id == "prior:0:A1"
        finally:
            await service.close()
