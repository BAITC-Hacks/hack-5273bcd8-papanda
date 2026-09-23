"""HTTP adapter for the deterministic domain and asynchronous AI service."""
from contextlib import asynccontextmanager
import os
from pathlib import Path
from uuid import uuid4
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from dotenv import load_dotenv
from app.contracts import (TaskCreate, AnalyzeRequest, AnswersRequest, CardUpdate,
                           ProposalCreate, DecisionRequest, StageRequest, CardField, Source)
from app.catalog import catalog
from app.store import Store, now

ACTIVE = {"analyzing", "waiting_answers", "building_card"}


def create_app(db_path=None, ai_mode=None):
    root = Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env")
    mode = ai_mode or os.getenv("AI_MODE", "stub")
    if mode not in {"stub", "engine", "light"}:
        raise ValueError("AI_MODE must be stub, engine or light")
    store = Store(str(db_path or os.getenv("SANA_DB") or os.getenv("DB_PATH") or root / "data" / "sana.db"))
    try:
        store.seed()
    except Exception:
        store.close()
        raise

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            try:
                if app.state.service is not None:
                    await app.state.service.close()
            finally:
                store.close()

    app = FastAPI(title="AI Sana Challenge Hub", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    app.state.service = None
    app.add_middleware(CORSMiddleware, allow_origin_regex=r"https?://(?:localhost|127\.0\.0\.1)(?::\d+)?", allow_methods=["*"], allow_headers=["*"])

    def service():
        if app.state.service is None:
            from app.ai.service import TaskRunService
            app.state.service = TaskRunService(store)
        return app.state.service

    def assert_editable(task_id):
        if app.state.service is not None and service().get(task_id).status in ACTIVE:
            raise ValueError("Дождитесь завершения AI-анализа перед изменением карточки")

    @app.exception_handler(KeyError)
    async def missing(request: Request, exc: KeyError):
        return JSONResponse(status_code=404, content={"detail": "Объект не найден"})

    @app.exception_handler(ValueError)
    async def invalid(request: Request, exc: ValueError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "ai_mode": "light" if mode == "engine" else mode, "version": 1}

    @app.post("/api/tasks", status_code=201)
    async def create_task(body: TaskCreate):
        return store.create_task(body.text, body.industry)

    @app.get("/api/tasks")
    async def tasks(): return store.list_tasks()

    @app.get("/api/tasks/{task_id}")
    async def task(task_id: str): return store.get_task(task_id)

    @app.post("/api/tasks/{task_id}/analyze", status_code=202)
    async def analyze(task_id: str, body: AnalyzeRequest | None = None):
        store.get_task(task_id)
        return service().start(task_id, mode=(body.mode if body else None) or mode)

    @app.get("/api/tasks/{task_id}/run")
    async def run(task_id: str):
        store.get_task(task_id)
        return service().get(task_id)

    @app.post("/api/tasks/{task_id}/answers", status_code=202)
    async def answers(task_id: str, body: AnswersRequest):
        store.get_task(task_id)
        return await service().submit_answers(task_id, body.answers)

    @app.put("/api/tasks/{task_id}/card")
    async def update_card(task_id: str, body: CardUpdate):
        task = store.get_task(task_id)
        assert_editable(task_id)
        changed = False
        for name, edit in body.fields.items():
            value = edit.value.strip()
            old = task.card.fields.get(name, CardField())
            status = "confirmed" if value and edit.confirmed else "edited" if value else "empty"
            if old.value == value and old.status == status:
                continue
            changed = True
            sources = old.sources
            if value != old.value or (value and not sources):
                source_id = "edit_" + uuid4().hex
                task.sources[source_id] = value
                sources = [Source(source_id=source_id, quote=value)] if value else []
            task.card.fields[name] = CardField(value=value, status=status, sources=sources)
        if changed:
            task.revision += 1
            task.status = "draft"
            task.published_at = None
        store.save_task(task)
        return task

    @app.post("/api/tasks/{task_id}/publish")
    async def publish(task_id: str):
        task = store.get_task(task_id)
        assert_editable(task_id)
        nonempty = [field for field in task.card.fields.values() if field.value.strip()]
        if not nonempty or any(field.status != "confirmed" for field in nonempty):
            raise ValueError("Перед публикацией подтвердите все непустые поля карточки")
        task.status = "published"
        task.published_at = task.published_at or now()
        store.save_task(task)
        return task

    @app.get("/api/catalog")
    async def get_catalog(topic: str | None = None, level: str | None = None):
        if level and level not in {"draft", "working", "ready", "priority"}:
            raise HTTPException(422, "Неизвестный уровень готовности")
        return catalog(store.list_tasks(), topic, level)

    @app.get("/api/teams")
    async def teams(): return store.list_teams()

    @app.get("/api/leaderboard")
    async def leaderboard(): return sorted(store.list_teams(), key=lambda t: (-t.points, t.name))

    @app.post("/api/tasks/{task_id}/proposals", status_code=201)
    async def proposal(task_id: str, body: ProposalCreate): return store.create_proposal(task_id, body)

    @app.get("/api/tasks/{task_id}/proposals")
    async def proposals(task_id: str): return store.list_proposals(task_id)

    @app.post("/api/proposals/{proposal_id}/decision")
    async def decision(proposal_id: str, body: DecisionRequest):
        proposal = store.get_proposal(proposal_id)
        if proposal.stage_confirmed and body.decision != "accept":
            raise ValueError("Нельзя отклонить команду после подтверждения её этапа")
        proposal.decision = body.decision
        store.save_proposal(proposal)
        return proposal

    @app.post("/api/proposals/{proposal_id}/stage")
    async def stage(proposal_id: str, body: StageRequest): return store.confirm_stage(proposal_id)

    @app.get("/api/ai/contract")
    async def ai_contract():
        from engines import get_engine
        from engines.common.contracts import StartRequest, RunView
        return {"engine": "light", "input_schema": StartRequest.model_json_schema(),
                "output_schema": RunView.model_json_schema(), **get_engine("light").contract()}

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str):
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "API endpoint not found")
        dist = (root / "web" / "dist").resolve()
        target = (dist / path).resolve()
        if not target.is_relative_to(dist):
            raise HTTPException(404, "File not found")
        if target.is_file():
            return FileResponse(target)
        if (dist / "index.html").is_file():
            return FileResponse(dist / "index.html")
        raise HTTPException(503, "Frontend is not built. Run npm run build in web.")

    return app
