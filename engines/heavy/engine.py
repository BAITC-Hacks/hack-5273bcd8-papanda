"""Bounded, independently judged domain engine. All graph changes pass atomic commits."""
import asyncio
import copy
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from engines.common.channel import AnswerChannel
from engines.common.contracts import Answer, CardDraft, RunView, StartRequest
from engines.common.json_output import extract_json
from engines.common.llm import ProviderError, build_role_clients
from engines.common.provenance import validate_card
from engines.common.trace import Trace
from .models import Move, PAYLOADS, Verdict, move_schema
from .state import State, Resolver, StructuralValidator, commit, observe, current_observations
from .validation import card_errors


PROMPTS = Path(__file__).parent / "prompts"
JUDGED = {"PROPOSE_SIMPLEST", "DEVELOP_PROCESS", "ESTABLISH_CONTRADICTION", "PROPOSE_LEAP", "ASSESS_PRACTICE", "REVISE_WORLD", "ASSESS_LEAP"}
CRITERIA = {
    "PROPOSE_SIMPLEST": "Процесс выведен из черновика и выражает порождающую потребность; не требуй будущих шагов.",
    "DEVELOP_PROCESS": "Развитие простейшего заложено в цитируемом источнике; развитие противоположности конкретизирует потребность команды, а не пересказывает бизнес.",
    "ESTABLISH_CONTRADICTION": "Единство и несовместимость либо пробел следуют из указанных развитий обеих сторон.",
    "PROPOSE_LEAP": "Вопросы разрешают указанное противоречие, каждый спрашивает один факт, не подсказывает ответ и не повторяет известное.",
    "ASSESS_PRACTICE": "Оценка соответствует реальному наблюдению. Неизвестный ответ не подтверждает ожидание; ошибка инструмента опровергает его.",
    "REVISE_WORLD": "Указанные опровергнутые наблюдения действительно требуют пересмотра; причина конкретна.",
    "ASSESS_LEAP": "Реализация разрешения подтверждается указанными успешными наблюдениями; не выдавай неизвестное за установленное.",
}
EXAMPLES = {
    "PROPOSE_SIMPLEST": {"content": "Сократить очередь обращений", "quote": "Сократить очередь обращений"},
    "ASSESS_SIMPLEST": {"accept": True, "reason": "Потребность прямо выражена в источнике"},
    "DEVELOP_PROCESS": {"source_process_id": "P1", "potential": "Уточнить доступные материалы", "emergence": "Проверка известного ограничения", "concretization": "Материалы пока отсутствуют", "quote": "Материалы пока отсутствуют", "source_id": "draft"},
    "ESTABLISH_CONTRADICTION": {"simplest_dev_refs": ["D1"], "opposite_dev_refs": ["D2"], "unity": "Команде нужны входные материалы, их наличие не определено", "kind": "gap"},
    "PROPOSE_LEAP": {"contradiction_id": "C1", "content": "Уточнить стартовые условия", "outcome": "mediation", "questions": [{"text": "Какие материалы доступны команде?", "field": "data", "why": "Закрыть пробел входных данных"}]},
    "ASSESS_PRACTICE": {"observation_id": "O1", "relation": "inconclusive", "explanation": "Бизнес ответил, что пока не знает", "consequence": "Оставить неизвестное пустым"},
    "REVISE_WORLD": {"observation_ids": ["O1"], "reason": "Ответ опроверг исходное предположение"},
    "ASSESS_LEAP": {"resolution_id": "R1", "observation_ids": ["O1", "O2"], "explanation": "Материалы уточнены, цитаты проверены"},
    "BEGIN_EXECUTION": {"resolution_ids": ["R1"]},
    "ASK_BUSINESS": {"origin_process_id": "P5", "expectation": "Получить сведения бизнеса по плану вопросов"},
    "COMMIT_CARD": {"origin_process_id": "P5", "expectation": "Проверить источники", "card": {"fields": {key: {"value": None, "sources": []} for key in ("title", "context", "need", "users", "data", "constraints", "expected_result", "success_criteria", "contact", "interaction_format")}}},
    "COMPLETE": {"reason": "Практика оценена, источники проверены"},
}


class StopRun(Exception):
    def __init__(self, reason, message):
        self.reason, self.message = reason, message


@dataclass
class Running:
    run_id: str
    state: State
    trace: Trace
    status: str = "analyzing"
    pending: list = field(default_factory=list)
    task: asyncio.Task | None = None
    started: float = field(default_factory=time.monotonic)
    paused: float = 0
    waiting_since: float | None = None
    metrics: dict = field(default_factory=lambda: {"llm_calls": 0, "tokens_by_role": {}, "rejections": 0, "judge_unavailable": 0, "iterations": 0, "rounds": 0})
    stage_rejections: dict = field(default_factory=dict)
    last_rejection: str = ""
    repeated: int = 0
    error: str | None = None
    stop_reason: str | None = None
    last_view: RunView | None = None

    def elapsed(self):
        until = self.waiting_since if self.waiting_since is not None else time.monotonic()
        return max(0, until - self.started - self.paused)


class HeavyEngine:
    name = "heavy"

    def __init__(self, actor=None, judge=None, *, channel=None, answer_timeout_s=None,
                 max_iterations=40, max_rejections=12, stage_rejections=4,
                 deadline_s=240, max_llm_calls=100, model_timeout_s=60):
        if actor is None or judge is None:
            default_actor, default_judge = build_role_clients()
            actor, judge = actor or default_actor, judge or default_judge
        if actor is judge or (getattr(actor, "model", None) and getattr(actor, "model", None) == getattr(judge, "model", None)):
            raise ValueError("Актор и судья должны быть разными моделями")
        self.actor, self.judge = actor, judge
        timeout = answer_timeout_s if answer_timeout_s is not None else float(os.getenv("ANSWER_TIMEOUT_S", "900"))
        self.channel = channel or AnswerChannel(timeout_s=timeout)
        self.max_iterations, self.max_rejections = max_iterations, max_rejections
        self.stage_rejections, self.deadline_s = stage_rejections, deadline_s
        self.max_llm_calls, self.model_timeout_s = max_llm_calls, model_timeout_s
        self.runs = {}

    async def start(self, req: StartRequest) -> str:
        run_id = uuid4().hex
        run = Running(run_id=run_id, state=State(req=req.model_copy(deep=True)), trace=Trace(run_id))
        self.runs[run_id] = run
        self._publish(run, "run_started", request=req.model_dump())
        run.task = asyncio.create_task(self._run(run))
        return run_id

    def view(self, run_id: str) -> RunView:
        return self.runs[run_id].last_view.model_copy(deep=True)

    def trace(self, run_id):
        return copy.deepcopy(self.runs[run_id].trace.events)

    def sources(self, run_id):
        return dict(self.runs[run_id].state.sources)

    async def submit_answers(self, run_id: str, answers: list[Answer]) -> None:
        run = self.runs[run_id]
        if run.status != "waiting_answers":
            raise ValueError("Прогон не ожидает ответы")
        expected = {q.question_id for q in run.pending}
        ids = [a.question_id for a in answers]
        if set(ids) != expected or len(ids) != len(expected):
            raise ValueError("Нужен один ответ на каждый ожидаемый вопрос; пустой текст допустим")
        self.channel.submit(run_id, [a.model_copy(deep=True) for a in answers])

    async def cancel(self, run_id):
        run = self.runs[run_id]
        if run.status in {"card_ready", "failed", "cancelled"}:
            return
        self.channel.cancel(run_id)
        run.task.cancel()
        try:
            await run.task
        except asyncio.CancelledError:
            pass
        if run.status != "cancelled":
            self._finish(run, "cancelled", "cancelled", "Прогон отменён")

    async def close(self):
        await asyncio.gather(*(self.cancel(rid) for rid in list(self.runs)))

    def contract(self):
        rejected = [e for r in self.runs.values() for e in r.trace.events if e["event"] == "json_invalid"]
        return {"engine": self.name, "prompts": {p.stem: p.read_text(encoding="utf-8") for p in PROMPTS.glob("*.md")},
                "move_schemas": {name: model.model_json_schema() for name, model in PAYLOADS.items()},
                "judge_schema": Verdict.model_json_schema(), "invalid_response_examples": rejected[:3],
                "validation": "Структурные предусловия, независимый судья, цитаты через validate_card; отказ не попадает в граф",
                "budgets": {"iterations": self.max_iterations, "rejections_total": self.max_rejections, "rejections_per_move": self.stage_rejections, "deadline_s": self.deadline_s, "max_question_rounds": 2}}

    def _publish(self, run, event, **payload):
        run.metrics.update(elapsed_s=round(run.elapsed(), 4), rounds=run.state.rounds)
        run.last_view = RunView(run_id=run.run_id, task_id=run.state.req.task_id, engine="heavy",
            status=run.status, pending_questions=run.pending, card=run.state.card if run.status == "card_ready" else None,
            graph=run.state.graph(), error=run.error, stop_reason=run.stop_reason, metrics=copy.deepcopy(run.metrics))
        run.trace.add(event, **payload, view=run.last_view.model_dump())

    def _finish(self, run, status, reason=None, error=None):
        run.status, run.stop_reason, run.error = status, reason, error
        run.pending = []
        if status in {"failed", "cancelled"}:
            run.state.phase = "failed"
        self._publish(run, "run_finished", status=status, stop_reason=reason)

    def _budget(self, run):
        if run.elapsed() >= self.deadline_s:
            raise StopRun("deadline", "Истёк бюджет времени анализа; прогресс сохранён")
        if run.metrics["llm_calls"] >= self.max_llm_calls:
            raise StopRun("budget", "Исчерпан бюджет вызовов модели")

    async def _generate(self, run, role, messages, schema):
        self._budget(run)
        client = self.actor if role == "actor" else self.judge
        timeout = min(self.model_timeout_s, self.deadline_s - run.elapsed())
        started = time.monotonic()
        run.metrics["llm_calls"] += 1
        try:
            result = await asyncio.wait_for(client.generate(messages, json_schema=schema, max_tokens=2200, timeout=timeout), timeout=timeout)
        except asyncio.TimeoutError as exc:
            if run.elapsed() >= self.deadline_s:
                raise StopRun("deadline", "Истёк бюджет времени анализа") from exc
            raise ProviderError("Превышено время ожидания модели") from exc
        except ProviderError:
            self._publish(run, "llm_call", role=role, duration_s=time.monotonic()-started, success=False)
            raise
        tokens = run.metrics["tokens_by_role"].setdefault(role, {})
        for key, count in result.usage.items():
            if isinstance(count, (int, float)):
                tokens[key] = tokens.get(key, 0) + count
        self._publish(run, "llm_call", role=role, duration_s=time.monotonic()-started, usage=result.usage, success=True)
        return result.text

    async def _parse(self, run, role, text, schema, model):
        for attempt in range(2):
            try:
                parsed = model.model_validate(extract_json(text))
                if isinstance(parsed, Move):
                    parsed.typed()
                return parsed
            except (ValueError, TypeError, ValidationError) as exc:
                self._publish(run, "json_invalid", role=role, response=text, error=str(exc), repair_attempt=attempt)
                if attempt:
                    raise ValueError("Некорректный JSON после одной попытки исправления") from exc
                text = await self._generate(run, role, [
                    {"role": "system", "content": "Исправь только формат JSON по данной схеме. Не добавляй факты. Верни один объект."},
                    {"role": "user", "content": json.dumps({"schema": schema, "invalid": text, "error": str(exc)}, ensure_ascii=False)}], schema)

    def _actor_prompt(self, run, allowed):
        s = run.state
        state = {"phase": s.phase, "candidate": s.candidate, "simplest": s.simplest, "opposite": s.opposite,
            "processes": s.processes, "developments": s.developments, "contradictions": s.contradictions,
            "resolutions": {k: v for k, v in s.resolutions.items() if not v.get("superseded")},
            "roadmap": {k: v for k, v in s.roadmaps.get(s.current_map, {}).items() if k != "frozen_copy"},
            "actions": {k: v for k, v in s.actions.items() if v["roadmap_id"] == s.current_map},
            "assessments": {k: v for k, v in s.assessments.items() if v["observation_id"] in {o["id"] for o in current_observations(s)}},
            "sources": {k: v for k, v in s.sources.items() if k != "draft"}, "rounds": s.rounds}
        hint = "Следующий достижимый шаг: " + ", ".join(allowed)
        if "DEVELOP_PROCESS" in allowed and not any(d["side"] == "opposite" for d in s.developments.values()):
            hint = f"Развей {'противоположность '+str(s.opposite) if s.opposite else 'простейшее '+str(s.simplest)}; обе стороны нужны для противоречия."
        feedback = run.last_rejection
        if run.repeated >= 2:
            feedback = "СТОП: повторный отказ. " + feedback + "; выбери из " + ", ".join(allowed)
        samples = {m: {"move": m, "payload": EXAMPLES[m]} for m in allowed if run.stage_rejections.get(m, 0) >= 2 and m in EXAMPLES}
        payload = {"draft": s.req.draft, "state": state, "observations": current_observations(s),
            "last_rejection": feedback, "next_step": hint, "allowed": allowed,
            "remaining_iterations": self.max_iterations-run.metrics["iterations"],
            "schemas": move_schema(allowed), "generic_examples_not_user_facts": samples}
        return [{"role": "system", "content": (PROMPTS/"actor.md").read_text(encoding="utf-8")},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]

    async def _judge(self, run, move):
        payload = {"move": move.model_dump(), "criterion": CRITERIA[move.move],
            "contract": PAYLOADS[move.move].model_json_schema(), "draft": run.state.req.draft,
            "graph": run.state.graph().model_dump(), "development_refs": run.state.developments,
            "actions": {k: v for k, v in run.state.actions.items() if v["roadmap_id"] == run.state.current_map},
            "observations": current_observations(run.state)}
        messages = [{"role": "system", "content": (PROMPTS/"judge.md").read_text(encoding="utf-8")},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
        schema = Verdict.model_json_schema()
        text = await self._generate(run, "judge", messages, schema)
        return await self._parse(run, "judge", text, schema, Verdict)

    def _reject(self, run, name, reason, *, unavailable=False):
        self._publish(run, "rejected", move=name, reason="judge_unavailable" if unavailable else reason)
        if unavailable:
            run.metrics["judge_unavailable"] += 1
            return
        feedback = f"{name}: {reason}"
        run.repeated = run.repeated + 1 if feedback == run.last_rejection else 1
        run.last_rejection = feedback
        run.metrics["rejections"] += 1
        run.stage_rejections[name] = run.stage_rejections.get(name, 0) + 1
        if run.metrics["rejections"] >= self.max_rejections or run.stage_rejections[name] >= self.stage_rejections:
            raise StopRun("budget", "Исчерпан бюджет отказов; неподтверждённые ходы не применены")

    async def _tool(self, run, name):
        action_id = next(reversed(run.state.actions))
        if name == "ASK_BUSINESS":
            run.pending = [run.state.questions[q] for q in run.state.actions[action_id]["args"]["question_ids"]]
            run.status = "waiting_answers"
            run.waiting_since = time.monotonic()
            self._publish(run, "questions_asked", questions=[q.model_dump() for q in run.pending])
            try:
                answers = await self.channel.ask(run.run_id, run.pending)
            except asyncio.TimeoutError:
                run.state = observe(run.state, action_id, False, {}, "Истекло время ожидания ответа бизнеса")
                raise StopRun("answer_timeout", "Истекло время ожидания ответа бизнеса")
            finally:
                run.paused += time.monotonic() - run.waiting_since
                run.waiting_since = None
            payload = {"answers": [a.model_dump() for a in answers]}
            run.state = observe(run.state, action_id, True, payload)
            run.pending, run.status = [], "building_card"
            self._publish(run, "answers_received", answers=payload["answers"], sources=run.state.sources)
        else:
            card = CardDraft.model_validate(run.state.actions[action_id]["args"])
            errors = card_errors(card, run.state)
            run.state = observe(run.state, action_id, not errors, {"errors": errors},
                "Проверка источников не пройдена" if errors else None, card=card if not errors else None)
            self._publish(run, "card_validated", success=not errors, errors=errors)
            if run.state.provenance_failures >= 3:
                raise StopRun("provenance_failed", "Три карточки не прошли проверку источников")

    async def _run(self, run):
        try:
            for iteration in range(self.max_iterations):
                self._budget(run)
                run.metrics["iterations"] = iteration + 1
                allowed = Resolver.allowed(run.state)
                if not allowed:
                    raise StopRun("no_allowed_moves", "Нет допустимых ходов; прогресс сохранён")
                schema = move_schema(allowed)
                text = await self._generate(run, "actor", self._actor_prompt(run, allowed), schema)
                try:
                    move = await self._parse(run, "actor", text, schema, Move)
                except ValueError as exc:
                    self._reject(run, "INVALID_JSON", str(exc))
                    continue
                self._publish(run, "proposal", proposal=move.model_dump())
                ok, error = StructuralValidator.validate(move, run.state)
                if not ok:
                    self._reject(run, move.move, error)
                    continue
                if move.move in JUDGED:
                    try:
                        verdict = await self._judge(run, move)
                    except (ProviderError, ValueError):
                        self._reject(run, move.move, "", unavailable=True)
                        continue
                    if not verdict.accepted:
                        self._reject(run, move.move, "; ".join(verdict.issues) or verdict.reason)
                        continue
                old_opposite = run.state.opposite
                run.state = commit(move, run.state)
                run.last_rejection, run.repeated = "", 0
                self._publish(run, "committed", move=move.move)
                if not old_opposite and run.state.opposite:
                    self._publish(run, "committed", move="DESIGNATE_OPPOSITE", by="code", process_id=run.state.opposite)
                if move.move in {"ASK_BUSINESS", "COMMIT_CARD"}:
                    await self._tool(run, move.move)
                if run.state.phase == "done":
                    self._finish(run, "card_ready")
                    return
            raise StopRun("budget", "Исчерпан бюджет итераций; прогресс сохранён")
        except StopRun as exc:
            self._finish(run, "failed", exc.reason, exc.message)
        except ProviderError as exc:
            self._finish(run, "failed", "provider_unavailable", str(exc))
        except asyncio.CancelledError:
            self._finish(run, "cancelled", "cancelled", "Прогон отменён")
            raise
        except Exception as exc:
            self._finish(run, "failed", "internal_error", f"Ошибка движка: {type(exc).__name__}: {exc}")
