"""Strict move payloads. New entity IDs are allocated by the commit layer only."""
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from engines.common.contracts import CardDraft, FieldName


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


Text = Annotated[str, Field(min_length=1)]


class Propose(Strict):
    content: Text
    quote: Text


class AssessSimplest(Strict):
    accept: bool
    reason: Text


class Develop(Strict):
    source_process_id: Text
    potential: Text
    emergence: Text
    concretization: Text
    quote: str = ""
    source_id: str = "draft"


class Contradict(Strict):
    simplest_dev_refs: list[str] = Field(min_length=1)
    opposite_dev_refs: list[str] = Field(min_length=1)
    unity: Text
    kind: Literal["gap", "conflict"]


class PlannedQuestion(Strict):
    text: str = Field(min_length=1, max_length=200)
    field: FieldName
    why: Text


class Leap(Strict):
    contradiction_id: Text
    content: Text
    outcome: Literal["replacement", "mediation"]
    questions: list[PlannedQuestion] = Field(min_length=1, max_length=5)


class Begin(Strict):
    resolution_ids: list[str] = Field(min_length=1)


class Ask(Strict):
    origin_process_id: Text
    expectation: Text


class CommitCard(Ask):
    card: CardDraft


class AssessPractice(Strict):
    observation_id: Text
    relation: Literal["confirmed", "partially_confirmed", "contradicted", "inconclusive"]
    explanation: Text
    consequence: Text


class Revise(Strict):
    observation_ids: list[str] = Field(min_length=1)
    reason: Text


class AssessLeap(Strict):
    resolution_id: Text
    observation_ids: list[str] = Field(min_length=1)
    explanation: Text


class Complete(Strict):
    reason: Text


PAYLOADS = {
    "PROPOSE_SIMPLEST": Propose, "ASSESS_SIMPLEST": AssessSimplest,
    "DEVELOP_PROCESS": Develop, "ESTABLISH_CONTRADICTION": Contradict,
    "PROPOSE_LEAP": Leap, "BEGIN_EXECUTION": Begin,
    "ASK_BUSINESS": Ask, "COMMIT_CARD": CommitCard,
    "ASSESS_PRACTICE": AssessPractice, "REVISE_WORLD": Revise,
    "ASSESS_LEAP": AssessLeap, "COMPLETE": Complete,
}


class Move(Strict):
    move: str
    payload: dict

    def typed(self):
        if self.move not in PAYLOADS:
            raise ValueError("Неизвестный ход")
        return PAYLOADS[self.move].model_validate(self.payload)


class Verdict(Strict):
    accepted: bool
    reason: str
    issues: list[str]


def move_schema(allowed: list[str]) -> dict:
    # Keep definitions scoped inside each branch: provider accepts JSON Schema.
    variants = []
    for name in allowed:
        payload = PAYLOADS[name].model_json_schema()
        variants.append({"type": "object", "properties": {
            "move": {"const": name}, "payload": payload},
            "required": ["move", "payload"], "additionalProperties": False})
    # Pydantic refs are root-relative, so hoist shared definitions.
    definitions = {}
    for variant in variants:
        definitions.update(variant["properties"]["payload"].pop("$defs", {}))
    result = {"oneOf": variants}
    if definitions:
        result["$defs"] = definitions
    return result
