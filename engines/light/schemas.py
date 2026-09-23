"""Strict fixed-pipeline proposals, newly implemented for the Sana contract."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from engines.common.contracts import CardDraft, FieldName

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")

class BusinessElement(Strict):
    id: str
    dimension: str
    statement: str
    quote: str

class StudentElement(Strict):
    id: str
    dimension: str
    statement: str

class Simplest(Strict):
    content: str
    elements: list[BusinessElement]

class Opposite(Strict):
    content: str
    need: str
    elements: list[StudentElement]

class Contradiction(Strict):
    id: str
    kind: Literal["gap", "conflict"]
    field: FieldName
    simplest_refs: list[str]
    opposite_refs: list[str]
    statement: str

class PlannedQuestion(Strict):
    id: str
    text: str = Field(max_length=200)
    field: FieldName
    contradiction_id: str
    why: str

class Analysis(Strict):
    simplest: Simplest
    opposite: Opposite
    contradictions: list[Contradiction]
    questions: list[PlannedQuestion]

class Issue(Strict):
    target_id: str
    problem: str

class Critique(Strict):
    accepted: bool
    issues: list[Issue] = Field(default_factory=list)

class Assessment(Strict):
    contradiction_id: str
    relation: Literal["resolved", "not_resolved", "new_gap"]
    explanation: str

class AssessedCard(Strict):
    assessment: list[Assessment]
    card: CardDraft

class Followup(Strict):
    questions: list[PlannedQuestion] = Field(min_length=1, max_length=3)
