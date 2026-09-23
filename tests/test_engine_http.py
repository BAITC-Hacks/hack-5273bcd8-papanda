"""Frozen HTTP contract serves an independently injected offline engine."""
import asyncio
import json

import httpx
import pytest

from engines import _instances
from engines.common.llm import ScriptedLLM
from engines.light import LightEngine
from engines.service import app
from test_light_engine import DRAFT, WEIGHTS, analysis_fixture, card_fixture
from test_heavy_engine import DRAFT as HEAVY_DRAFT, WEIGHTS as HEAVY_WEIGHTS, make_heavy


@pytest.mark.asyncio
async def test_engine_http_lifecycle(monkeypatch):
    engine = LightEngine(
        actor=ScriptedLLM([json.dumps(analysis_fixture(), ensure_ascii=False),
                           json.dumps(card_fixture(), ensure_ascii=False)]),
        judge=ScriptedLLM(['{"accepted":true,"issues":[]}']), max_rounds=1,
    )
    monkeypatch.setitem(_instances, "light", engine)
    monkeypatch.setenv("ENGINE", "light")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/engine/runs", json={"task_id": "t", "draft": DRAFT,
                                                          "field_weights": WEIGHTS})
        assert created.status_code == 202
        rid = created.json()["run_id"]
        for _ in range(1000):
            view = (await client.get(f"/engine/runs/{rid}")).json()
            if view["status"] == "waiting_answers":
                break
            await asyncio.sleep(0.001)
        assert view["status"] == "waiting_answers"
        assert len(view["pending_questions"]) >= 3
        submitted = await client.post(f"/engine/runs/{rid}/answers", json={"answers": [
            {"question_id": q["question_id"], "text": "Доступен CSV" if q["field"] == "data" else "Не знаю"}
            for q in view["pending_questions"]]})
        assert submitted.status_code == 202
        for _ in range(1000):
            view = (await client.get(f"/engine/runs/{rid}")).json()
            if view["status"] == "card_ready":
                break
            await asyncio.sleep(0.001)
        assert view["status"] == "card_ready"
        assert view["card"]["fields"]["data"]["value"] == "Доступен CSV"
        assert (await client.get(f"/engine/runs/{rid}/trace")).status_code == 200
        contract = (await client.get("/engine/contract?engine=light")).json()
        assert "prompts" in contract and "input_schema" in contract
        assert (await client.get("/engine/health")).status_code == 200


@pytest.mark.asyncio
async def test_heavy_engine_http_lifecycle(monkeypatch):
    engine = make_heavy()
    monkeypatch.setitem(_instances, "heavy", engine)
    monkeypatch.setenv("ENGINE", "heavy")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/engine/runs", json={"task_id": "t", "draft": HEAVY_DRAFT,
                                                          "field_weights": HEAVY_WEIGHTS})
        assert created.status_code == 202
        rid = created.json()["run_id"]
        for _ in range(1000):
            view = (await client.get(f"/engine/runs/{rid}")).json()
            if view["status"] == "waiting_answers":
                break
            await asyncio.sleep(0.001)
        assert view["status"] == "waiting_answers"
        answers = {"data": "Есть список вопросов", "users": "Клиенты",
                   "success_criteria": "Меньше звонков"}
        response = await client.post(f"/engine/runs/{rid}/answers", json={"answers": [
            {"question_id": q["question_id"], "text": answers[q["field"]]}
            for q in view["pending_questions"]]})
        assert response.status_code == 202
        for _ in range(1000):
            view = (await client.get(f"/engine/runs/{rid}")).json()
            if view["status"] == "card_ready":
                break
            await asyncio.sleep(0.001)
        assert view["status"] == "card_ready"
        assert view["card"]["fields"]["data"]["value"] == "Есть список вопросов"
        assert "prompts" in (await client.get("/engine/contract?engine=heavy")).json()
        trace = await client.get(f"/engine/runs/{rid}/trace")
        assert trace.status_code == 200
        assert trace.headers["content-type"].startswith("application/x-ndjson")
        assert any(json.loads(line)["event"] == "run_finished" for line in trace.text.splitlines())
