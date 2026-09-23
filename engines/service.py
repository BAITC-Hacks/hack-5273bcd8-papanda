"""Thin HTTP adapter for the frozen engine contract."""
from fastapi import FastAPI, HTTPException

from . import get_engine
from .common.contracts import AnswersPayload, StartRequest

app = FastAPI(title="AI Sana dialectical engines")


def locate(run_id: str):
    for name in ("light", "heavy"):
        engine = get_engine(name)
        try:
            engine.view(run_id)
            return engine
        except KeyError:
            continue
    raise HTTPException(404, "Прогон не найден")


@app.post("/engine/runs", status_code=202)
async def start(req: StartRequest):
    return {"run_id": await get_engine().start(req)}


@app.get("/engine/runs/{run_id}")
def view(run_id: str):
    return locate(run_id).view(run_id)


@app.post("/engine/runs/{run_id}/answers", status_code=202)
async def submit_answers(run_id: str, payload: AnswersPayload):
    engine = locate(run_id)
    await engine.submit_answers(run_id, payload.answers)
    return engine.view(run_id)


@app.delete("/engine/runs/{run_id}")
async def cancel(run_id: str):
    engine = locate(run_id)
    await engine.cancel(run_id)
    return engine.view(run_id)


@app.get("/engine/runs/{run_id}/trace")
def trace(run_id: str):
    return locate(run_id).trace(run_id)


@app.get("/engine/contract")
def contract(engine: str = "light"):
    from .common.contracts import StartRequest, RunView
    selected = get_engine(engine)
    return {"engine": engine, "input_schema": StartRequest.model_json_schema(),
            "output_schema": RunView.model_json_schema(),
            "prompts": selected.prompts(), "malformed_example": selected.malformed_example()}


@app.get("/engine/health")
def health():
    from .common.llm import provider_health
    return provider_health()
