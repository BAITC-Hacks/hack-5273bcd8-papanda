"""Network-free scripts and protocol regression tests for the new heavy engine."""
import asyncio
import copy
import json
from typing import get_args

import pytest

from engines.common.contracts import Answer, CardDraft, FieldName, RunView, StartRequest
from engines.common.llm import ProviderError, ScriptedLLM
from engines.heavy.engine import HeavyEngine
from engines.heavy.models import Move, move_schema
from engines.heavy.state import State, Resolver, StructuralValidator, commit, observe


DRAFT = "Хотим чат-бота для клиентов. Данных пока нет."
WEIGHTS = {key: 10 for key in get_args(FieldName)}


def proposal(move, **payload):
    return {"move": move, "payload": payload}


def script(case=None):
    case = case or {"draft": DRAFT, "answers_by_field": {"data": "Есть список вопросов", "users": "Клиенты", "success_criteria": "Меньше звонков"}}
    draft = case["draft"]
    selected = ["data", "users", "success_criteria"]
    card = {key: {"value": None, "sources": []} for key in get_args(FieldName)}
    card["context"] = {"value": draft, "sources": [{"source_id": "draft", "quote": draft}]}
    for i, key in enumerate(selected, 1):
        text = case.get("answers_by_field", {}).get(key, "")
        if text and text.lower().strip() not in {"не знаю", "неизвестно"} and key not in case.get("unknown_fields", []):
            card[key] = {"value": text, "sources": [{"source_id": f"A{i}", "quote": text}]}
    moves = [
        proposal("PROPOSE_SIMPLEST", content=draft, quote=draft),
        proposal("ASSESS_SIMPLEST", accept=True, reason="Потребность названа бизнесом"),
        proposal("DEVELOP_PROCESS", source_process_id="P1", potential="Уточнение условий", emergence="Конкретизация потребности", concretization=draft, quote=draft),
        proposal("DEVELOP_PROCESS", source_process_id="P3", potential="Самостоятельный старт команды", emergence="Необходимость входных сведений", concretization="Команде нужны данные, пользователи и критерий успеха"),
        proposal("ESTABLISH_CONTRADICTION", simplest_dev_refs=["D1"], opposite_dev_refs=["D2"], unity="Для самостоятельного старта не хватает условий", kind="gap"),
        proposal("PROPOSE_LEAP", contradiction_id="C1", content="Уточнить стартовые условия", outcome="mediation", questions=[
            {"text": "Какие материалы можно предоставить?", "field": "data", "why": "Закрыть пробел материалов"},
            {"text": "Кто будет пользоваться решением?", "field": "users", "why": "Уточнить аудиторию"},
            {"text": "Какой признак покажет успех?", "field": "success_criteria", "why": "Уточнить критерий приёмки"}]),
        proposal("BEGIN_EXECUTION", resolution_ids=["R1"]),
        proposal("ASK_BUSINESS", origin_process_id="P5", expectation="Получить сведения бизнеса"),
        proposal("ASSESS_PRACTICE", observation_id="O1", relation="partially_confirmed", explanation="Получены доступные сведения; неизвестное остаётся неизвестным", consequence="Заполнить лишь известные поля"),
        proposal("COMMIT_CARD", origin_process_id="P5", expectation="Карточка имеет проверяемые цитаты", card={"fields": card}),
        proposal("ASSESS_PRACTICE", observation_id="O2", relation="confirmed", explanation="Код подтвердил источники", consequence="Оценить реализацию разрешения"),
        proposal("ASSESS_LEAP", resolution_id="R1", observation_ids=["O1", "O2"], explanation="Вопросы заданы, известное зафиксировано, неизвестное оставлено пустым"),
        proposal("COMPLETE", reason="Практика проверена, черновик карточки готов человеку"),
    ]
    return moves


def make_heavy(case=None, scenario="happy", **kwargs):
    moves = script(case)
    if scenario == "malformed":
        actor = ["{broken"] + [json.dumps(m, ensure_ascii=False) for m in moves]
    else:
        actor = [json.dumps(m, ensure_ascii=False) for m in moves]
    judge = [json.dumps({"accepted": True, "reason": "Критерий соблюдён", "issues": []})] * 50
    return HeavyEngine(ScriptedLLM(actor), ScriptedLLM(judge), **kwargs)


def req(task="task", draft=DRAFT):
    return StartRequest(task_id=task, draft=draft, field_weights=WEIGHTS)


async def until(engine, rid, statuses):
    for _ in range(500):
        view = engine.view(rid)
        if view.status in statuses:
            return view
        await asyncio.sleep(.001)
    raise AssertionError(engine.view(rid))


async def answer(engine, rid, texts=None):
    view = await until(engine, rid, {"waiting_answers", "failed"})
    assert view.status == "waiting_answers", view
    texts = texts or ["Есть список вопросов", "Клиенты", "Меньше звонков"]
    await engine.submit_answers(rid, [Answer(question_id=q.question_id, text=t) for q, t in zip(view.pending_questions, texts)])
    return await until(engine, rid, {"card_ready", "failed", "cancelled"})


@pytest.mark.asyncio
async def test_happy_real_observations_and_sources():
    engine = make_heavy()
    rid = await engine.start(req())
    first = await until(engine, rid, {"waiting_answers", "failed"})
    assert len(first.pending_questions) == 3
    assert {q.points_at_stake for q in first.pending_questions} == {10}
    assert all(q.contradiction_id in {n.id for n in first.graph.nodes} for q in first.pending_questions)
    result = await answer(engine, rid)
    assert result.status == "card_ready", result
    assert result.card.fields["contact"].value is None
    assert result.card.fields["data"].sources[0].source_id == "A1"
    RunView.model_validate(result.model_dump())
    events = engine.trace(rid)
    assert any(e.get("move") == "DESIGNATE_OPPOSITE" and e.get("by") == "code" for e in events)
    assert {e["event"] for e in events} >= {"questions_asked", "answers_received", "card_validated", "run_finished"}
    before = len(engine.actor.calls)
    engine.view(rid)
    assert len(engine.actor.calls) == before


@pytest.mark.asyncio
async def test_malformed_json_repair_visible_in_contract():
    engine = make_heavy(scenario="malformed")
    rid = await engine.start(req())
    assert (await answer(engine, rid)).status == "card_ready"
    assert engine.contract()["invalid_response_examples"][0]["response"] == "{broken"


@pytest.mark.asyncio
async def test_judge_unavailable_does_not_poison_prompts_or_budget():
    moves = script()
    actor = ScriptedLLM([json.dumps(m) for m in [moves[0]] + moves])
    judge = ScriptedLLM([ProviderError("judge_unavailable")] + [json.dumps({"accepted": True, "reason": "Да", "issues": []})] * 50)
    engine = HeavyEngine(actor, judge)
    rid = await engine.start(req())
    result = await answer(engine, rid)
    assert result.status == "card_ready", result
    assert result.metrics["judge_unavailable"] == 1
    assert result.metrics["rejections"] == 0
    assert all("judge_unavailable" not in str(c["messages"]) for c in actor.calls + judge.calls)


@pytest.mark.asyncio
async def test_answer_timeout_preserves_graph():
    engine = make_heavy(answer_timeout_s=.005)
    rid = await engine.start(req())
    result = await until(engine, rid, {"failed"})
    assert result.stop_reason == "answer_timeout"
    assert result.graph.nodes
    assert result.card is None


@pytest.mark.asyncio
async def test_cancellation_and_independent_runs():
    first, second = make_heavy(), make_heavy()
    a, b = await first.start(req("first")), await second.start(req("second"))
    await until(first, a, {"waiting_answers"})
    await until(second, b, {"waiting_answers"})
    await first.cancel(a)
    assert first.view(a).status == "cancelled"
    assert second.view(b).status == "waiting_answers"
    assert (await answer(second, b)).status == "card_ready"


@pytest.mark.asyncio
async def test_stage_rejection_budget_and_examples():
    initial = script()[0]
    engine = HeavyEngine(ScriptedLLM([json.dumps(initial)] * 4), ScriptedLLM([json.dumps({"accepted": False, "reason": "Исправь", "issues": ["content должен выражать потребность"]})] * 4))
    rid = await engine.start(req())
    result = await until(engine, rid, {"failed"})
    assert result.stop_reason == "budget"
    assert result.metrics["rejections"] == 4
    assert not result.graph.nodes
    assert "СТОП" in str(engine.actor.calls[2]["messages"])
    assert "generic_examples_not_user_facts" in str(engine.actor.calls[2]["messages"])


@pytest.mark.asyncio
async def test_actor_provider_failure_is_honest():
    engine = HeavyEngine(ScriptedLLM([ProviderError("Провайдер недоступен")]), ScriptedLLM([]))
    rid = await engine.start(req())
    result = await until(engine, rid, {"failed"})
    assert result.stop_reason == "provider_unavailable"
    assert result.card is None


def planning_state():
    s = State(req())
    for raw in script()[:7]:
        s = commit(Move.model_validate(raw), s)
    return s


def test_resolver_does_not_offer_premature_moves_and_seed_is_code():
    s = State(req())
    assert Resolver.allowed(s) == ["PROPOSE_SIMPLEST"]
    for raw in script()[:3]:
        s = commit(Move.model_validate(raw), s)
    assert s.opposite == "P3"
    assert "ESTABLISH_CONTRADICTION" not in Resolver.allowed(s)
    assert "ASK_BUSINESS" not in Resolver.allowed(s)
    assert "DESIGNATE_OPPOSITE" not in Resolver.allowed(s)


def test_rejected_candidate_is_not_in_graph_and_commit_is_atomic():
    s = State(req())
    s = commit(Move.model_validate(script()[0]), s)
    assert s.candidate and not s.graph().nodes
    before = copy.deepcopy(s)
    with pytest.raises(ValueError):
        commit(Move.model_validate(script()[7]), s)
    assert s == before
    s = commit(Move(move="ASSESS_SIMPLEST", payload={"accept": False, "reason": "Не подходит"}), s)
    assert not s.processes and not s.designations


@pytest.mark.parametrize("index,change", [
    (2, {"source_process_id": "MISSING"}),
    (2, {"quote": "Несуществующая цитата"}),
    (4, {"simplest_dev_refs": ["D2"]}),
    (4, {"opposite_dev_refs": ["D1"]}),
    (5, {"contradiction_id": "C404"}),
    (6, {"resolution_ids": ["R404"]}),
    (7, {"origin_process_id": "P404"}),
])
def test_structural_refs_reject_atomically(index, change):
    s = State(req())
    moves = script()
    for raw in moves[:index]:
        s = commit(Move.model_validate(raw), s)
    invalid = copy.deepcopy(moves[index])
    invalid["payload"].update(change)
    before = copy.deepcopy(s)
    assert not StructuralValidator.validate(Move.model_validate(invalid), s)[0]
    with pytest.raises(ValueError):
        commit(Move.model_validate(invalid), s)
    assert s == before


def test_failed_tool_requires_contradicted_and_revision_reopens_route():
    s = planning_state()
    s = commit(Move.model_validate(script()[7]), s)
    s = observe(s, "T1", True, {"answers": [{"question_id": "Q1", "text": "не знаю"}]})
    s = commit(Move.model_validate(script()[8]), s)
    s = commit(Move.model_validate(script()[9]), s)
    s = observe(s, "T2", False, {"errors": ["Источника нет"]}, "Ошибка")
    assert not StructuralValidator.validate(Move.model_validate(script()[10]), s)[0]
    s = commit(Move(move="ASSESS_PRACTICE", payload={"observation_id": "O2", "relation": "contradicted", "explanation": "Источника нет", "consequence": "Пересмотр"}), s)
    assert {"REVISE_WORLD", "COMMIT_CARD"}.issubset(Resolver.allowed(s))
    old_map = copy.deepcopy(s.roadmaps["M1"])
    s = commit(Move(move="REVISE_WORLD", payload={"observation_ids": ["O2"], "reason": "Требуется новое уточнение"}), s)
    assert s.phase == "planning" and s.roadmaps["M1"] == old_map
    assert "REVISE_WORLD" not in Resolver.allowed(s)
    assert "ASK_BUSINESS" not in Resolver.allowed(s)
    assert "PROPOSE_LEAP" in Resolver.allowed(s)


@pytest.mark.asyncio
async def test_provenance_trap_never_returns_fabricated_card():
    moves = script()
    invalid = copy.deepcopy(moves[9])
    invalid["payload"]["card"]["fields"]["data"]["value"] = "Есть 987654 пользователей"
    moves[9] = invalid
    moves[10] = proposal("ASSESS_PRACTICE", observation_id="O2", relation="contradicted", explanation="Выдуманное число", consequence="Исправить карточку")
    moves.insert(11, script()[9])
    moves.insert(12, proposal("ASSESS_PRACTICE", observation_id="O3", relation="confirmed", explanation="Цитаты проверены", consequence="Оценить скачок"))
    moves[13]["payload"]["observation_ids"] = ["O1", "O3"]
    engine = HeavyEngine(ScriptedLLM([json.dumps(m) for m in moves]), ScriptedLLM([json.dumps({"accepted": True, "reason": "Да", "issues": []})]*50))
    rid = await engine.start(req())
    result = await answer(engine, rid)
    assert result.status == "card_ready", result
    assert "987654" not in result.card.model_dump_json()
    checks = [e for e in engine.trace(rid) if e["event"] == "card_validated"]
    assert [e["success"] for e in checks] == [False, True]


def test_only_allowed_move_schemas_are_exposed():
    schema = move_schema(["PROPOSE_SIMPLEST"])
    assert len(schema["oneOf"]) == 1
    assert schema["oneOf"][0]["properties"]["move"]["const"] == "PROPOSE_SIMPLEST"


@pytest.mark.parametrize("field,value,source,quote,code", [
    ("context", "Чат-бот нужен бизнесу", "draft", DRAFT, "extractive_value"),
    ("context", "Данных", "draft", "Данных", "complete_quote"),
    ("contact", DRAFT, "draft", DRAFT, "manual_contact"),
    ("users", "Есть список вопросов", "A1", "Есть список вопросов", "answer_field"),
])
def test_product_constraints(field, value, source, quote, code):
    from engines.heavy.validation import card_errors
    s = planning_state()
    s = commit(Move.model_validate(script()[7]), s)
    s = observe(s, "T1", True, {"answers": [{"question_id": "Q1", "text": "Есть список вопросов"}]})
    card = CardDraft.model_validate({"fields": {field: {"value": value, "sources": [{"source_id": source, "quote": quote}]}}})
    assert code in {e["code"] for e in card_errors(card, s)}


@pytest.mark.asyncio
async def test_three_provenance_failures_stop_honestly():
    moves = script()[:9]
    for i in range(3):
        bad = copy.deepcopy(script()[9])
        bad["payload"]["card"]["fields"]["data"]["value"] = f"Придумано {987000+i} клиентов"
        moves.append(bad)
        moves.append(proposal("ASSESS_PRACTICE", observation_id=f"O{i+2}", relation="contradicted", explanation="Проверка источников не прошла", consequence="Исправить выдумку"))
    engine = HeavyEngine(ScriptedLLM([json.dumps(m) for m in moves]), ScriptedLLM([json.dumps({"accepted": True, "reason": "Да", "issues": []})]*50))
    rid = await engine.start(req())
    result = await answer(engine, rid)
    assert result.status == "failed" and result.stop_reason == "provenance_failed"
    assert result.card is None and result.graph.nodes


@pytest.mark.asyncio
async def test_same_engine_parallel_states_are_isolated():
    class RoutedActor:
        def __init__(self):
            self.models = {d: ScriptedLLM([json.dumps(m) for m in script({"draft": d})]) for d in (DRAFT, "Нужен сервис консультаций.")}

        async def generate(self, messages, **kwargs):
            draft = json.loads(messages[-1]["content"])["draft"]
            return await self.models[draft].generate(messages, **kwargs)

    engine = HeavyEngine(RoutedActor(), ScriptedLLM([json.dumps({"accepted": True, "reason": "Да", "issues": []})]*50))
    first = await engine.start(req("first"))
    second = await engine.start(req("second", "Нужен сервис консультаций."))
    await until(engine, first, {"waiting_answers"})
    await until(engine, second, {"waiting_answers"})
    await engine.cancel(first)
    assert engine.view(second).status == "waiting_answers"
    view = await answer(engine, second)
    assert view.status == "card_ready" and view.task_id == "second"
    assert engine.sources(second)["draft"] != engine.sources(first)["draft"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs,reason", [({"deadline_s": .000001}, "deadline"), ({"max_iterations": 3}, "budget")])
async def test_time_and_iteration_budgets(kwargs, reason):
    engine = make_heavy(**kwargs)
    rid = await engine.start(req())
    view = await until(engine, rid, {"failed"})
    assert view.stop_reason == reason and view.card is None


def test_question_round_cap_after_revisions_and_complete_needs_practice():
    s = planning_state()
    assert "COMPLETE" not in Resolver.allowed(s)
    for round_number in range(2):
        roadmap = s.roadmaps[s.current_map]
        pid = roadmap["execution_process_ids"][0]
        s = commit(Move(move="ASK_BUSINESS", payload={"origin_process_id": pid, "expectation": "Уточнить сведения"}), s)
        aid = next(reversed(s.actions))
        s = observe(s, aid, True, {"answers": [{"question_id": q, "text": "не знаю"} for q in roadmap["question_ids"]]})
        oid = next(reversed(s.observations))
        s = commit(Move(move="ASSESS_PRACTICE", payload={"observation_id": oid, "relation": "contradicted", "explanation": "Ожидание уточнения не подтвердилось", "consequence": "Пересмотреть карту"}), s)
        s = commit(Move(move="REVISE_WORLD", payload={"observation_ids": [oid], "reason": "Сведения остаются неизвестными"}), s)
        s = commit(Move.model_validate(script()[5]), s)
        rid = next(reversed(s.resolutions))
        s = commit(Move(move="BEGIN_EXECUTION", payload={"resolution_ids": [rid]}), s)
    assert s.rounds == 2
    assert "ASK_BUSINESS" not in Resolver.allowed(s)
    assert "COMMIT_CARD" in Resolver.allowed(s)
    assert "COMPLETE" not in Resolver.allowed(s)
    assert len(s.roadmaps) == 3
