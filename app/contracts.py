"""Shared contract v1. Changes are coordinated by the integrator."""
from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldName(str, Enum):
    title = "title"
    context = "context"
    need = "need"
    users = "users"
    data = "data"
    constraints = "constraints"
    expected_result = "expected_result"
    success_criteria = "success_criteria"
    contact = "contact"
    interaction_format = "interaction_format"


FIELD_LABELS = {
    "title": "Название", "context": "Контекст", "need": "Потребность",
    "users": "Пользователи", "data": "Данные и материалы", "constraints": "Ограничения",
    "expected_result": "Ожидаемый результат", "success_criteria": "Критерии успеха",
    "contact": "Контакт", "interaction_format": "Формат взаимодействия",
}
FIELD_WEIGHTS = {"title": 0, "context": 10, "need": 10, "users": 10, "data": 20,
                 "constraints": 10, "expected_result": 15, "success_criteria": 15,
                 "contact": 5, "interaction_format": 5}


class Source(Model):
    source_id: str
    quote: str


class CardField(Model):
    value: str = ""
    status: Literal["empty", "ai_proposed", "edited", "confirmed"] = "empty"
    sources: list[Source] = Field(default_factory=list)


class Card(Model):
    fields: dict[FieldName, CardField] = Field(default_factory=lambda: {
        field: CardField() for field in FieldName})


class Breakdown(Model):
    criterion: str
    field: FieldName
    points: float
    max: float
    reason: str


class Missing(Model):
    field: FieldName
    max_gain: float
    hint: str


class Score(Model):
    total: float = 0
    level: Literal["draft", "working", "ready", "priority"] = "draft"
    breakdown: list[Breakdown] = Field(default_factory=list)
    missing: list[Missing] = Field(default_factory=list)


class Task(Model):
    id: str
    text: str
    industry: str
    status: Literal["draft", "published"] = "draft"
    card: Card = Field(default_factory=Card)
    score: Score = Field(default_factory=Score)
    created_at: str
    published_at: str | None = None
    sources: dict[str, str] = Field(default_factory=dict)
    revision: int = 0


class Question(Model):
    answer_id: str
    field: FieldName
    question: str
    why: str
    points_at_stake: float = 0
    node_id: str | None = None


class Answer(Model):
    answer_id: str
    answer: str = Field(default="", max_length=10000)


class Team(Model):
    id: str
    name: str
    interests: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    points: int = 0


class Proposal(Model):
    id: str
    task_id: str
    team_id: str
    idea: str
    plan: str
    deadline: str
    link: str
    decision: Literal["pending", "accept", "reject"] = "pending"
    stage_confirmed: bool = False
    created_at: str


class GraphNode(Model):
    id: str
    kind: str
    label: str
    source_ids: list[str] = Field(default_factory=list)
    status: str = "proposed"


class GraphEdge(Model):
    source: str
    target: str
    label: str = ""


class Graph(Model):
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)


class RunState(Model):
    task_id: str
    run_id: str = ""
    mode: Literal["stub", "engine", "replay", "light", "heavy"] = "stub"
    status: Literal["idle", "analyzing", "waiting_answers", "building_card", "card_ready", "error"] = "idle"
    stop_reason: str | None = None
    pending_questions: list[Question] = Field(default_factory=list)
    graph: Graph = Field(default_factory=Graph)
    card_draft: Card | None = None
    error: str | None = None
    calls: int = 0
    elapsed_seconds: float = 0
    tokens: dict[str, int] = Field(default_factory=dict)


class TaskCreate(Model):
    text: str = Field(min_length=5, max_length=20000)
    industry: str = Field(min_length=1, max_length=120)


class AnalyzeRequest(Model):
    mode: Literal["stub", "engine", "light", "heavy"] | None = None


class AnswersRequest(Model):
    answers: list[Answer] = Field(min_length=1, max_length=5)


class FieldEdit(Model):
    value: str = Field(max_length=20000)
    confirmed: bool = False


class CardUpdate(Model):
    fields: dict[FieldName, FieldEdit]


class ProposalCreate(Model):
    team_id: str
    idea: str = Field(min_length=5, max_length=10000)
    plan: str = Field(min_length=5, max_length=10000)
    deadline: str = Field(min_length=1, max_length=200)
    link: str = Field(max_length=2000)


class DecisionRequest(Model):
    decision: Literal["accept", "reject"]


class StageRequest(Model):
    confirmed: Literal[True]
