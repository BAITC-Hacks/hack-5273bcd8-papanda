"""Frozen interface between the AI Sana product and either reasoning engine."""
from typing import Literal

from pydantic import BaseModel, Field, field_validator


FieldName = Literal[
    "title", "context", "need", "users", "data", "constraints",
    "expected_result", "success_criteria", "contact", "interaction_format",
]


class StartRequest(BaseModel):
    task_id: str
    draft: str
    industry: str | None = None
    field_weights: dict[FieldName, int]
    language: Literal["ru"] = "ru"


class Question(BaseModel):
    question_id: str
    text: str = Field(max_length=200)
    field: FieldName
    contradiction_id: str
    why: str
    points_at_stake: int


class Answer(BaseModel):
    question_id: str
    text: str = ""


class Source(BaseModel):
    source_id: str
    quote: str


class CardField(BaseModel):
    value: str | None = None
    sources: list[Source] = Field(default_factory=list)


class CardDraft(BaseModel):
    fields: dict[FieldName, CardField] = Field(default_factory=dict)


class GraphNode(BaseModel):
    id: str
    kind: Literal["simplest", "opposite", "element", "contradiction", "question", "answer", "field"]
    label: str
    status: Literal["open", "resolved", "reopened", "rejected", "ok"] | None = None
    side: Literal["simplest", "opposite"] | None = None


class GraphEdge(BaseModel):
    source: str
    target: str
    kind: Literal["develops", "contradicts", "resolves", "asks", "answers", "grounds"]


class GraphView(BaseModel):
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)


RunStatus = Literal["analyzing", "waiting_answers", "building_card", "card_ready", "failed", "cancelled"]


class RunView(BaseModel):
    run_id: str
    task_id: str
    engine: Literal["light", "heavy"]
    status: RunStatus
    pending_questions: list[Question] = Field(default_factory=list)
    card: CardDraft | None = None
    graph: GraphView = Field(default_factory=GraphView)
    error: str | None = None
    stop_reason: str | None = None
    metrics: dict = Field(default_factory=dict)


class AnswersPayload(BaseModel):
    answers: list[Answer]
