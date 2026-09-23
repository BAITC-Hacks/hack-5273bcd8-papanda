"""New domain dialectical world → opposing prerequisites → judged gaps → questions.

No historical engine code is used. All model business assertions and final values
are extractive. Interpretive candidates are visibly marked hypotheses.
"""
import asyncio
import json
import os
import time
import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from app.contracts import Card, CardField, Source, GraphNode, GraphEdge, Question, FieldName, FIELD_WEIGHTS
from .prompts import SYSTEM
from .validation import validate_field, validate_card
from .events import emit, SchemaFailure, SourceFailure, SemanticRejection, BudgetExhausted

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")

class Assertion(Strict):
    id: str
    value: str
    sources: list[Source]

class Candidate(Strict):
    id: str
    kind: str
    field: FieldName
    interpretation: str
    evidence: list[str]

class World(Strict):
    assertions: list[Assertion] = Field(max_length=30)
    business_need: str
    student_prerequisites: str
    candidates: list[Candidate] = Field(max_length=15)

class Judgment(Strict):
    accepted: list[str]
    explanation: str

class Ask(Strict):
    candidate_id: str
    question: str

class Questions(Strict):
    questions: list[Ask] = Field(min_length=3, max_length=5)

class CardJudgment(Strict):
    accepted: bool
    explanation: str


def config():
    key = os.getenv("AI_ACTOR_API_KEY", os.getenv("OPENAI_API_KEY", ""))
    actor = os.getenv("AI_ACTOR_MODEL", "")
    judge = os.getenv("AI_JUDGE_MODEL", "")
    if not key or not os.getenv("AI_JUDGE_API_KEY", os.getenv("OPENAI_API_KEY", "")) or not actor or not judge:
        raise ValueError("Configure OPENAI_API_KEY, AI_ACTOR_MODEL and AI_JUDGE_MODEL")
    if actor == judge:
        raise ValueError("Actor and judge model names must differ")
    return key, actor, judge


async def call(service, task_id, role, instruction, schema, payload):
    state, ctx = service.runs[task_id], service.contexts[task_id]
    key, actor, judge = config()
    max_calls = min(12, max(1, int(os.getenv("AI_MAX_CALLS", "12"))))
    max_seconds = min(240.0, max(1.0, float(os.getenv("AI_MAX_SECONDS", "180"))))
    spent = ctx.get("provider_seconds", 0.0)
    if state.calls >= max_calls or spent >= max_seconds:
        raise BudgetExhausted("Hard model call/time budget exhausted")
    model = actor if role == "actor" else judge
    key = os.getenv(f"AI_{role.upper()}_API_KEY", os.getenv("OPENAI_API_KEY", ""))
    base_url = os.getenv(f"AI_{role.upper()}_BASE_URL", os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    state.calls += 1
    emit(service, task_id, "model_request", role=role, model=model, stage=instruction.split(":")[0], input=payload, call=state.calls)
    started = time.monotonic()
    try:
        async with asyncio.timeout(max_seconds - spent), httpx.AsyncClient(timeout=min(45.0, max_seconds - spent)) as client:
            response = await client.post(base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {key}"}, json={"model": model,
                    "messages": [{"role": "system", "content": SYSTEM + "\n" + instruction + "\nJSON schema: " + json.dumps(schema.model_json_schema())},
                                 {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                    "response_format": {"type": "json_object"}})
            response.raise_for_status()
            result = response.json()
            for name, value in result.get("usage", {}).items():
                if isinstance(value, int) and not isinstance(value, bool):
                    state.tokens[name] = state.tokens.get(name, 0) + value
            try:
                content = result["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise SchemaFailure("Provider returned no choices/message/content") from exc
            emit(service, task_id, "model_response", role=role, output=content, usage=result.get("usage", {}))
    finally:
        ctx["provider_seconds"] = spent + time.monotonic() - started
    try:
        parsed = schema.model_validate_json(content)
    except (ValidationError, ValueError, TypeError) as exc:
        evidence = {"kind": "schema", "failure": str(exc), "invalid_output": content}
        emit(service, task_id, "validation_failed", **evidence)
        reserve_correction(service, task_id, evidence)
        return await call(service, task_id, role, instruction + " CORRECTION: repair JSON using the actual validation error; do not add business facts.", schema,
                          {**payload, "correction": evidence})
    emit(service, task_id, "schema_validation_passed", schema=schema.__name__)
    return parsed


def reserve_correction(service, task_id, evidence):
    ctx = service.contexts[task_id]
    if ctx.get("corrections", 0) >= 1:
        kind = evidence.get("kind")
        error = SemanticRejection if kind == "semantic" else SourceFailure if kind == "source" else SchemaFailure
        raise error("One correction was already attempted; validation still fails")
    ctx["corrections"] = 1
    emit(service, task_id, "correction_requested", evidence=evidence)


def validate_world(world, sources):
    ids = [a.id for a in world.assertions] + [c.id for c in world.candidates]
    if len(ids) != len(set(ids)) or any(not i or i in {"world", "business", "student"} for i in ids):
        raise ValueError("Unique graph node IDs required")
    assertion_ids = {a.id for a in world.assertions}
    for assertion in world.assertions:
        if not assertion.value.strip():
            raise ValueError("Empty assertion")
        validate_field(CardField(value=assertion.value, sources=assertion.sources), sources)
    for candidate in world.candidates:
        if candidate.kind not in {"gap", "ambiguity", "contradiction"} or candidate.field == FieldName.contact:
            raise ValueError("Invalid candidate kind or manual field")
        if not candidate.evidence or not set(candidate.evidence) <= assertion_ids:
            raise ValueError("Candidate must reference source-grounded assertions")
        if candidate.kind == "contradiction" and len(set(candidate.evidence)) < 2:
            raise ValueError("Contradiction needs two distinct assertions")


def graph_world(state, world, accepted, revision):
    prefix = f"r{revision}:"
    if not state.graph.nodes:
        state.graph.nodes.append(GraphNode(id="world", kind="world", label="Мир задачи", status="developing"))
    business, student = prefix + "business", prefix + "student"
    state.graph.nodes.extend([
        GraphNode(id=business, kind="business_need", label=world.business_need, status="interpretation_not_fact"),
        GraphNode(id=student, kind="student_prerequisites", label=world.student_prerequisites, status="interpretation_not_fact")])
    state.graph.edges.extend([GraphEdge(source="world", target=business, label="потребность бизнеса"),
        GraphEdge(source=business, target=student, label="сопоставлена с условиями выполнения")])
    for assertion in world.assertions:
        state.graph.nodes.append(GraphNode(id=prefix+assertion.id, kind="assertion", label=assertion.value,
            source_ids=[s.source_id for s in assertion.sources], status="user_supplied"))
        state.graph.edges.append(GraphEdge(source=business, target=prefix+assertion.id, label="основание"))
    for candidate in world.candidates:
        state.graph.nodes.append(GraphNode(id=prefix+candidate.id, kind=candidate.kind, label=candidate.interpretation,
            status="needs_human_verification" if candidate.id in accepted else "judge_rejected"))
        for evidence in candidate.evidence:
            state.graph.edges.append(GraphEdge(source=prefix+evidence, target=prefix+candidate.id, label="основание гипотезы"))
        state.graph.edges.append(GraphEdge(source=student, target=prefix+candidate.id, label="проверка выполнимости"))
    return prefix


async def advance(service, task_id):
    state, ctx = service.runs[task_id], service.contexts[task_id]
    config()
    revision = ctx.get("world_revision", 0)
    if revision > 2:
        raise ValueError("World revision budget exhausted")
    # The previous world remains inspectable; new answers revise it instead of looping.
    payload = {"sources": ctx["sources"], "round": ctx["round"], "previous_world": ctx.get("world"),
               "answered_fields": list(ctx["answers"])}
    world = await call(service, task_id, "actor",
        "BUILD_WORLD then DEVELOP_OPPOSITION. Assertions must be entire sources or complete sentences cited verbatim, preserving punctuation and negation. Business need and student prerequisites are explicitly interpretations. Discover missing prerequisites, ambiguity or actual conflicting assertions. Never force contradictions. Include useful gap candidates if information is missing. Do not ask personal/contact questions. Reassess previous world against new answers; do not repeat answered-field questions.", World, payload)
    try:
        validate_world(world, ctx["sources"])
    except ValueError as exc:
        emit(service, task_id, "validation_failed", kind="source", failure=str(exc))
        raise SourceFailure(str(exc)) from exc
    judgment = await call(service, task_id, "judge",
        "JUDGE candidates: accept only useful, source-grounded gaps/ambiguities or plausible contradictions. Do not invent contradiction for scoring. Return accepted candidate IDs only and a short methodological explanation.", Judgment,
        {"sources": ctx["sources"], "world": world.model_dump()})
    candidate_map = {c.id: c for c in world.candidates}
    if len(judgment.accepted) != len(set(judgment.accepted)) or not set(judgment.accepted) <= set(candidate_map):
        raise ValueError("Judge referenced unknown candidates")
    prefix = graph_world(state, world, judgment.accepted, revision)
    emit(service, task_id, "world_revision", revision=revision, graph=state.graph.model_dump(), judgment=judgment.model_dump())
    ctx["world"] = world.model_dump()
    ctx["world_revision"] = revision + 1
    eligible = {cid: candidate_map[cid] for cid in judgment.accepted if candidate_map[cid].field.value not in ctx["answers"]}
    if ctx["round"] < 2 and len(eligible) >= 3:
        asks = await call(service, task_id, "actor",
            "ASK_HUMAN: choose 3 to 5 distinct accepted candidates. Ask concise open clarification questions without presupposing unprovided facts. Reference candidate_id. Never ask contact or personal characteristics.", Questions,
            {"sources": ctx["sources"], "candidates": [c.model_dump() for c in eligible.values()]})
        ids = [q.candidate_id for q in asks.questions]
        if len(ids) != len(set(ids)) or not set(ids) <= set(eligible):
            raise ValueError("Questions must reference distinct eligible candidates")
        fields = [eligible[cid].field for cid in ids]
        if len(fields) != len(set(fields)):
            raise ValueError("Questions must target distinct card fields")
        ctx["round"] += 1
        state.pending_questions = [Question(answer_id=f"{state.run_id}:{ctx['round']}:{i}",
            field=eligible[q.candidate_id].field, question=q.question,
            why=eligible[q.candidate_id].interpretation, points_at_stake=FIELD_WEIGHTS[eligible[q.candidate_id].field.value],
            node_id=prefix+q.candidate_id) for i, q in enumerate(asks.questions)]
        state.status = "waiting_answers"
        state.stop_reason = "human_input_required"
        return
    if ctx["round"] == 0:
        raise ValueError("Initial analysis did not produce at least three grounded questions")
    card_payload = {"sources": ctx["sources"], "world": world.model_dump()}
    while True:
        card = await call(service, task_id, "actor",
            "COMMIT_CARD: extract only complete sources or complete sentences quoted verbatim into appropriate fields; preserve negation and punctuation. Concatenate multiple cited quotes using spaces. No paraphrasing, invented title or inferred numbers. Missing/conflicting facts stay empty. Contact must be empty. Every nonempty value must have citations and status ai_proposed. Human confirmation happens later. If correction evidence exists, correct only its identified failure.", Card,
            card_payload)
        try:
            validate_card(card, ctx["sources"])
        except ValueError as exc:
            evidence = {"kind": "source", "failure": str(exc), "invalid_output": card.model_dump()}
            emit(service, task_id, "validation_failed", **evidence)
            reserve_correction(service, task_id, evidence)
            card_payload = {**card_payload, "correction": evidence}
            continue
        emit(service, task_id, "card_source_validation_passed", card=card.model_dump())
        review = await call(service, task_id, "judge",
            "REASSESS proposed card. Check field relevance, unresolved contradictions and faithfully represented answers. Reject inappropriate assignments or unsupported interpretation. Citations prove source provenance, not objective truth. Do not require missing information to be invented.", CardJudgment,
            {"sources": ctx["sources"], "world": world.model_dump(), "card": card.model_dump()})
        emit(service, task_id, "card_semantic_judgment", judgment=review.model_dump())
        if review.accepted:
            break
        evidence = {"kind": "semantic", "failure": review.explanation, "invalid_output": card.model_dump()}
        reserve_correction(service, task_id, evidence)
        card_payload = {**card_payload, "correction": evidence}
    service._commit(task_id, card)
