"""Small SQLite repository. Stage awards use a single write transaction."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from threading import RLock
from uuid import uuid4
from app.contracts import Task, Team, Proposal, ProposalCreate
from app.scoring import score


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=15)
        with self._db:
            for table in ("tasks", "teams", "proposals"):
                self._db.execute(f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, body TEXT NOT NULL)")

    def close(self):
        with self._lock:
            self._db.close()

    def _get(self, table, model, id):
        with self._lock:
            row = self._db.execute(f"SELECT body FROM {table} WHERE id=?", (id,)).fetchone()
        if row is None:
            raise KeyError(id)
        return model.model_validate_json(row[0])

    def _list(self, table, model):
        with self._lock:
            rows = self._db.execute(f"SELECT body FROM {table} ORDER BY rowid").fetchall()
        return [model.model_validate_json(row[0]) for row in rows]

    def _save(self, table, item):
        with self._lock, self._db:
            self._db.execute(f"INSERT INTO {table}(id,body) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body", (item.id, item.model_dump_json()))

    def create_task(self, text, industry):
        text, industry = text.strip(), industry.strip()
        if len(text) < 5 or not industry:
            raise ValueError("Добавьте описание задачи и отрасль")
        task = Task(id=uuid4().hex, text=text, industry=industry, created_at=now(), sources={"draft": text})
        self.save_task(task)
        return task

    def get_task(self, id): return self._get("tasks", Task, id)
    def list_tasks(self): return self._list("tasks", Task)
    def save_task(self, task):
        task.score = score(task.card)
        self._save("tasks", task)
    def get_team(self, id): return self._get("teams", Team, id)
    def list_teams(self): return self._list("teams", Team)
    def save_team(self, team): self._save("teams", team)
    def get_proposal(self, id): return self._get("proposals", Proposal, id)
    def save_proposal(self, proposal): self._save("proposals", proposal)

    def create_proposal(self, task_id, data: ProposalCreate):
        task = self.get_task(task_id)
        self.get_team(data.team_id)
        if task.status != "published":
            raise ValueError("Отклики принимаются после публикации задачи")
        if len(data.idea.strip()) < 5 or len(data.plan.strip()) < 5 or not data.deadline.strip():
            raise ValueError("Заполните идею, план и срок")
        if data.link and not data.link.startswith(("https://", "http://")):
            raise ValueError("Ссылка должна начинаться с https:// или http://")
        proposal = Proposal(id=uuid4().hex, task_id=task_id, created_at=now(), **data.model_dump())
        self.save_proposal(proposal)
        return proposal

    def list_proposals(self, task_id):
        self.get_task(task_id)
        return [item for item in self._list("proposals", Proposal) if item.task_id == task_id]

    def confirm_stage(self, proposal_id):
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            proposal = self.get_proposal(proposal_id)
            if proposal.decision != "accept":
                raise ValueError("Подтвердить этап можно только выбранной команде")
            if not proposal.stage_confirmed:
                team = self.get_team(proposal.team_id)
                team.points += 10
                proposal.stage_confirmed = True
                self._db.execute("UPDATE teams SET body=? WHERE id=?", (team.model_dump_json(), team.id))
                self._db.execute("UPDATE proposals SET body=? WHERE id=?", (proposal.model_dump_json(), proposal.id))
            return proposal

    def seed(self):
        path = Path(__file__).resolve().parent.parent / "seed" / "data.json"
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        tasks = [Task.model_validate(item) for item in data.get("tasks", [])]
        teams = [Team.model_validate(item) for item in data.get("teams", [])]
        proposals = [Proposal.model_validate(item) for item in data.get("proposals", [])]
        task_ids = {t.id for t in tasks} | {t.id for t in self.list_tasks()}
        team_ids = {t.id for t in teams} | {t.id for t in self.list_teams()}
        for proposal in proposals:
            if proposal.task_id not in task_ids or proposal.team_id not in team_ids:
                raise ValueError("Seed contains a proposal with an unknown task or team")
        with self._lock, self._db:
            for table, items in (("tasks", tasks), ("teams", teams), ("proposals", proposals)):
                for item in items:
                    if isinstance(item, Task):
                        item.score = score(item.card)
                    self._db.execute(f"INSERT OR IGNORE INTO {table}(id,body) VALUES (?,?)", (item.id, item.model_dump_json()))
