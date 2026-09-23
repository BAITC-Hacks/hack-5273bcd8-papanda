"""The new engine reaches the unchanged product card-confirmation boundary."""
import asyncio
import json
from unittest.mock import patch

import httpx
import pytest

from app.ai.service import TaskRunService
from app.api import create_app
from app.contracts import Answer as ProductAnswer, Task
from engines.common.llm import ScriptedLLM
from engines.light import LightEngine
from test_light_engine import DRAFT, analysis_fixture, card_fixture
from test_heavy_engine import DRAFT as HEAVY_DRAFT, make_heavy


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
async def test_heavy_engine_to_product_unconfirmed_card():
    engine = make_heavy()
    store = MemoryStore()
    store.task.text = HEAVY_DRAFT
    service = TaskRunService(store)
    with patch("engines.get_engine", return_value=engine):
        try:
            service.start("t", "heavy")
            waiting = await wait_for(service, "waiting_answers")
            assert len(waiting.pending_questions) >= 3
            answers = {"data": "Есть список вопросов", "users": "Клиенты",
                       "success_criteria": "Меньше звонков"}
            await service.submit_answers("t", [ProductAnswer(
                answer_id=q.answer_id, answer=answers[q.field.value])
                for q in waiting.pending_questions])
            ready = await wait_for(service, "card_ready")
            assert ready.mode == "heavy"
            assert ready.card_draft.fields["data"].value == "Есть список вопросов"
            assert ready.card_draft.fields["data"].status == "ai_proposed"
            assert ready.calls > 0
        finally:
            await service.close()


@pytest.mark.asyncio
async def test_heavy_engine_through_product_http_api(tmp_path):
    engine = make_heavy()
    app = create_app(db_path=tmp_path / "two-engines.sqlite3", ai_mode="stub")
    with patch("engines.get_engine", return_value=engine):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            task = (await client.post("/api/tasks", json={"text": HEAVY_DRAFT,
                                                         "industry": "services"})).json()
            task_id = task["id"]
            started = await client.post(f"/api/tasks/{task_id}/analyze", json={"mode": "heavy"})
            assert started.status_code == 202
            for _ in range(1000):
                run = (await client.get(f"/api/tasks/{task_id}/run")).json()
                if run["status"] == "waiting_answers":
                    break
                await asyncio.sleep(0.001)
            assert run["status"] == "waiting_answers"
            answers = {"data": "Есть список вопросов", "users": "Клиенты",
                       "success_criteria": "Меньше звонков"}
            submitted = await client.post(f"/api/tasks/{task_id}/answers", json={"answers": [
                {"answer_id": q["answer_id"], "answer": answers[q["field"]]}
                for q in run["pending_questions"]]})
            assert submitted.status_code == 202
            for _ in range(1000):
                run = (await client.get(f"/api/tasks/{task_id}/run")).json()
                if run["status"] == "card_ready":
                    break
                await asyncio.sleep(0.001)
            assert run["status"] == "card_ready"
            persisted = (await client.get(f"/api/tasks/{task_id}")).json()
            assert persisted["card"]["fields"]["data"]["value"] == "Есть список вопросов"
            assert persisted["card"]["fields"]["data"]["status"] == "ai_proposed"
        await app.state.service.close()
        app.state.store.close()
