"""Step-0 API scaffold. Domain agent replaces explicit 501 placeholders."""
from fastapi import FastAPI, HTTPException


def create_app(db_path=None, ai_mode=None):
    app = FastAPI(title="AI Sana Challenge Hub", version="0.1.0")

    @app.get("/api/health")
    def health():
        return {"status": "scaffold", "ai_mode": ai_mode or "stub", "version": 1}

    def pending():
        raise HTTPException(501, "Модуль ещё не реализован")

    routes = {
        "/tasks": ["GET", "POST"], "/tasks/{task_id}": ["GET"],
        "/tasks/{task_id}/analyze": ["POST"], "/tasks/{task_id}/run": ["GET"],
        "/tasks/{task_id}/answers": ["POST"], "/tasks/{task_id}/card": ["PUT"],
        "/tasks/{task_id}/publish": ["POST"], "/catalog": ["GET"],
        "/tasks/{task_id}/proposals": ["GET", "POST"],
        "/proposals/{proposal_id}/decision": ["POST"],
        "/proposals/{proposal_id}/stage": ["POST"], "/teams": ["GET"],
        "/leaderboard": ["GET"], "/ai/contract": ["GET"],
    }
    for route, methods in routes.items():
        for method in methods:
            app.add_api_route("/api" + route, pending, methods=[method], name=method + route)
    return app
