"""Record actual API responses for a synthetic end-to-end demonstration.

Engine mode is opt-in: DIALECTIC_RUN_LIVE=1. Never labels stub evidence as live AI.
Each attempt uses a new database; artifacts are ignored by Git.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from app.api import create_app
from make_seed import EXAMPLES


def run(mode, output, timeout):
    root = Path(__file__).resolve().parent.parent
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    load_dotenv(root / ".env")
    if mode == "engine" and os.getenv("DIALECTIC_RUN_LIVE") != "1":
        raise SystemExit("Set DIALECTIC_RUN_LIVE=1 to authorize live model requests.")
    begun = time.monotonic()
    recording = {"mode": mode, "synthetic": True, "events": [], "success": False}
    app = create_app(str(output / ("demo-" + uuid4().hex + ".db")), mode)
    industry, title, draft, values = EXAMPLES[0]
    # Explicit simulated business answer resolves the draft's missing-data problem.
    values = {**values, "data": "После проверки подтверждаем: доступен CSV с расписанием и 30 обезличенных вопросов клиентов."}
    latest = None
    with TestClient(app) as client:
        def request(method, path, body=None):
            response = client.request(method, path, json=body)
            data = response.json()
            recording["events"].append({"elapsed": round(time.monotonic()-begun, 3),
                "method": method, "path": path, "request": body, "status": response.status_code, "response": data})
            if not response.is_success:
                raise RuntimeError(f"{method} {path}: HTTP {response.status_code}")
            return data
        try:
            task = request("POST", "/api/tasks", {"text": draft, "industry": industry})
            tid = task["id"]
            request("POST", f"/api/tasks/{tid}/analyze", {"mode": mode})
            last_state = None
            while time.monotonic()-begun < timeout:
                latest = request("GET", f"/api/tasks/{tid}/run")
                if latest["status"] != last_state:
                    print(f"{round(time.monotonic()-begun,1)}s {latest['status']} calls={latest['calls']}", flush=True)
                    last_state = latest["status"]
                if latest["status"] == "waiting_answers":
                    answers = [{"answer_id": q["answer_id"], "answer": values.get(q["field"], title if q["field"] == "title" else "Пока не известно")}
                               for q in latest["pending_questions"]]
                    request("POST", f"/api/tasks/{tid}/answers", {"answers": answers})
                elif latest["status"] == "card_ready":
                    break
                elif latest["status"] == "error":
                    raise RuntimeError("AI stopped: " + str(latest["stop_reason"]))
                time.sleep(0.5)
            if not latest or latest["status"] != "card_ready":
                raise RuntimeError("Demo exceeded timeout")
            task = request("GET", f"/api/tasks/{tid}")
            # These two manual additions are user-authored synthetic facts, not model output.
            edits = {name: {"value": field["value"], "confirmed": bool(field["value"].strip())}
                     for name, field in task["card"]["fields"].items()}
            edits["title"] = {"value": title, "confirmed": True}
            edits["contact"] = {"value": values["contact"], "confirmed": True}
            task = request("PUT", f"/api/tasks/{tid}/card", {"fields": edits})
            request("POST", f"/api/tasks/{tid}/publish")
            catalog = request("GET", "/api/catalog")
            assert any(item["id"] == tid for item in catalog)
            team = request("GET", "/api/teams")[0]
            proposal = request("POST", f"/api/tasks/{tid}/proposals", {
                "team_id": team["id"], "idea": "Соберём помощника по расписанию на предоставленном CSV.",
                "plan": "Проверим данные, создадим прототип, сверим ответы с критериями бизнеса.",
                "deadline": "4 недели", "link": "https://example.test/demo-prototype"})
            request("POST", f"/api/proposals/{proposal['id']}/decision", {"decision": "accept"})
            request("POST", f"/api/proposals/{proposal['id']}/stage", {"confirmed": True})
            request("POST", f"/api/proposals/{proposal['id']}/stage", {"confirmed": True})
            leaderboard = request("GET", "/api/leaderboard")
            assert next(t for t in leaderboard if t["id"] == team["id"])["points"] == team["points"]+10
            recording.update(success=True, final_score=task["score"]["total"], run=latest)
        except Exception as exc:
            recording.update(error=str(exc), run=latest)
        finally:
            recording["elapsed"] = round(time.monotonic()-begun, 3)
            target = output / "recording.json"
            target.write_text(json.dumps(recording, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: recording.get(k) for k in ["mode", "success", "elapsed", "final_score", "error"]}, ensure_ascii=False))
    return 0 if recording["success"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["stub", "engine"], default="stub")
    parser.add_argument("--output", default="artifacts/demo")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    sys.exit(run(args.mode, args.output, args.timeout))
