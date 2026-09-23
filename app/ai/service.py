"""Bounded task runs with explicit deterministic stub and strict source gate."""
import asyncio
import os
import time
from uuid import uuid4
from app.contracts import (Answer, Card, CardField, FieldName, FIELD_WEIGHTS,
    GraphNode, GraphEdge, Question, RunState, Source)
from .validation import validate_card, contains_contact
from .events import emit, SchemaFailure, SourceFailure, SemanticRejection, BudgetExhausted

QUESTIONS = {
    "need": "Что именно нужно изменить в текущей работе?",
    "users": "Кто будет пользоваться результатом? Укажите роли без персональных данных.",
    "data": "Какие данные и материалы доступны команде и как получить доступ?",
    "constraints": "Какие сроки, ограничения и требования необходимо учесть?",
    "expected_result": "Какой конкретный результат должна передать команда?",
    "success_criteria": "По каким измеримым признакам вы примете результат?",
    "interaction_format": "Как будут проходить консультации и обратная связь?",
}


class TaskRunService:
    def __init__(self, store):
        self.store = store
        self.runs = {}
        self.contexts = {}
        self.jobs = {}
        self.deadlines = {}

    def get(self, task_id):
        self.store.get_task(task_id)
        self._expire(task_id)
        return self.runs.get(task_id, RunState(task_id=task_id))

    def _expire(self, task_id):
        state = self.runs.get(task_id)
        ctx = self.contexts.get(task_id)
        if state and ctx and state.status in {"analyzing", "building_card", "waiting_answers"} and time.monotonic() < ctx["deadline"]:
            # Windows event-loop clock resolution can invoke a timer slightly early.
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop:
                handle = self.deadlines.get(task_id)
                if handle:
                    handle.cancel()
                self.deadlines[task_id] = loop.call_later(
                    max(0.02, ctx["deadline"] - time.monotonic()), self._expire, task_id)
        if state and ctx and state.status in {"analyzing", "building_card", "waiting_answers"} and time.monotonic() >= ctx["deadline"]:
            state.status = "error"
            state.stop_reason = "run_timeout"
            state.error = "Время анализа истекло, включая ожидание ответов. Запустите новый анализ."
            state.pending_questions = []
            state.elapsed_seconds = round(time.monotonic() - ctx["started"], 3)
            job = self.jobs.get(task_id)
            if job and not job.done():
                job.cancel()
            emit(self, task_id, "run_expired", state=state.model_dump())

    def start(self, task_id, mode=None):
        task = self.store.get_task(task_id)
        if self.get(task_id).status in {"analyzing", "building_card", "waiting_answers"}:
            raise ValueError("A run is already active")
        mode = mode or os.getenv("AI_MODE", "stub")
        if mode not in {"stub", "engine"}:
            raise ValueError("Unsupported AI mode")
        contact = task.card.fields[FieldName.contact].value
        if contains_contact(task.text, contact):
            raise ValueError("Remove contact details from description; use the manual contact field")
        # Whitelist live provenance only. Historical edit_UUID sources may be old contacts.
        forbidden = {s.source_id for s in task.card.fields[FieldName.contact].sources}
        current_refs = {s.source_id for name, field in task.card.fields.items()
                        if name != FieldName.contact for s in field.sources}
        sources = {k: v for k, v in task.sources.items()
                   if (k.startswith("answer:") or k in current_refs) and k not in forbidden
                   and not contains_contact(v, contact)}
        sources["draft"] = task.text
        for name, field in task.card.fields.items():
            if name != FieldName.contact and field.value and field.status in {"edited", "confirmed"} and not contains_contact(field.value, contact):
                sources[f"field:{name.value}"] = field.value
        state = RunState(task_id=task_id, run_id=uuid4().hex, mode=mode, status="analyzing")
        self.runs[task_id] = state
        self.contexts[task_id] = dict(sources=sources, round=0, revision=task.revision,
            started=time.monotonic(), deadline=time.monotonic() + max(0.01, float(os.getenv("AI_RUN_TIMEOUT", "900"))), answers={}, base=task.card.model_copy(deep=True))
        if task_id in self.deadlines:
            self.deadlines[task_id].cancel()
        self.deadlines[task_id] = asyncio.get_running_loop().call_later(
            max(0.0, self.contexts[task_id]["deadline"] - time.monotonic()), self._expire, task_id)
        emit(self, task_id, "run_started", state=state.model_dump())
        self.jobs[task_id] = asyncio.create_task(self._advance(task_id))
        return state

    async def _advance(self, task_id):
        state, ctx = self.runs[task_id], self.contexts[task_id]
        try:
            if state.mode == "engine":
                from .engine import advance
                await advance(self, task_id)
            else:
                self._stub(task_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.status = "error"
            state.stop_reason = ("schema_error" if isinstance(exc, SchemaFailure) else
                "source_validation_error" if isinstance(exc, SourceFailure) else
                "semantic_judge_rejection" if isinstance(exc, SemanticRejection) else
                "budget_exhausted" if isinstance(exc, BudgetExhausted) else
                "validation_error" if isinstance(exc, ValueError) else "provider_error")
            emit(self, task_id, "run_error", kind=state.stop_reason, exception_type=type(exc).__name__)
            # Do not expose provider response/body or credentials.
            state.error = f"Анализ остановлен ({type(exc).__name__}). Исправьте ввод или проверьте настройки API."
        finally:
            state.elapsed_seconds = round(time.monotonic() - ctx["started"], 3)
            emit(self, task_id, "state_transition", state=state.model_dump())

    def _stub(self, task_id):
        state, ctx = self.runs[task_id], self.contexts[task_id]
        if not state.graph.nodes:
            state.graph.nodes.append(GraphNode(id="world", kind="world", label="Мир задачи: утверждения пользователя", status="stub"))
            state.graph.nodes.append(GraphNode(id="draft", kind="assertion", label=ctx["sources"]["draft"], source_ids=["draft"], status="user_supplied"))
            state.graph.edges.append(GraphEdge(source="world", target="draft", label="основан на"))
        candidates = [k for k in QUESTIONS if k not in ctx["answers"] and not ctx["base"].fields[FieldName(k)].value]
        if ctx["round"] < 2 and (len(candidates) >= 3 or ctx["round"] == 0):
            ctx["round"] += 1
            selected = candidates[:4] if len(candidates) == 7 else candidates[:5]
            if len(selected) < 3:
                selected += [k for k in QUESTIONS if k not in selected][:3-len(selected)]
            state.pending_questions = []
            for field in selected:
                node_id = f"gap:{field}"
                if not any(n.id == node_id for n in state.graph.nodes):
                    state.graph.nodes.append(GraphNode(id=node_id, kind="gap", label=f"Не уточнено: {field}", status="unknown"))
                    state.graph.edges.append(GraphEdge(source="world", target=node_id, label="требует уточнения"))
                state.pending_questions.append(Question(answer_id=f"{state.run_id}:{ctx['round']}:{field}", field=field,
                    question=QUESTIONS[field], why="Детерминированная заглушка: это поле ещё не заполнено; противоречия не анализировались.",
                    points_at_stake=FIELD_WEIGHTS[field], node_id=node_id))
            state.status = "waiting_answers"
            state.stop_reason = "human_input_required"
        else:
            card = Card()
            card.fields[FieldName.context] = CardField(value=ctx["sources"]["draft"], status="ai_proposed", sources=[Source(source_id="draft", quote=ctx["sources"]["draft"])])
            for field, source_id in ctx["answers"].items():
                value = ctx["sources"][source_id]
                if value.strip():
                    card.fields[FieldName(field)] = CardField(value=value, status="ai_proposed", sources=[Source(source_id=source_id, quote=value)])
            self._commit(task_id, card)

    def _commit(self, task_id, card):
        state, ctx = self.runs[task_id], self.contexts[task_id]
        self._expire(task_id)
        if state.status == "error":
            raise ValueError("Run expired before commit")
        validate_card(card, ctx["sources"])
        emit(self, task_id, "card_source_validation_passed", card=card.model_dump())
        task = self.store.get_task(task_id)
        if task.revision != ctx["revision"]:
            raise ValueError("Task changed; run is stale")
        changed = False
        for name, proposal in card.fields.items():
            answered = name.value in ctx["answers"]
            if name != FieldName.contact and proposal.value and (answered or task.card.fields[name].status not in {"edited", "confirmed"}):
                changed = changed or task.card.fields[name].value != proposal.value or task.card.fields[name].status != "ai_proposed"
                if answered and task.card.fields[name].value != proposal.value:
                    emit(self, task_id, "human_answer_revises_field", field=name.value, previous=task.card.fields[name].model_dump(), proposal=proposal.model_dump())
                task.card.fields[name] = proposal.model_copy(update={"status": "ai_proposed"})
        if changed and task.status == "published":
            task.status = "draft"
            task.published_at = None
        task.sources.update(ctx["sources"])
        task.revision += 1
        self.store.save_task(task)
        ctx["revision"] = task.revision
        state.card_draft = task.card.model_copy(deep=True)
        state.pending_questions = []
        state.status = "card_ready"
        state.stop_reason = "human_confirmation_required"

    async def submit_answers(self, task_id, answers: list[Answer]):
        state = self.get(task_id)
        if state.status != "waiting_answers":
            raise ValueError("Run is not waiting for answers")
        ctx = self.contexts[task_id]
        if self.store.get_task(task_id).revision != ctx["revision"]:
            raise ValueError("Task changed; answers are stale")
        pending = {q.answer_id: q for q in state.pending_questions}
        ids = [a.answer_id for a in answers]
        if len(set(ids)) != len(ids) or set(ids) != set(pending):
            raise ValueError("Supply each current answer_id exactly once (empty answers permitted)")
        contact = self.store.get_task(task_id).card.fields[FieldName.contact].value
        if any(contains_contact(a.answer, contact) for a in answers):
            raise ValueError("Remove contact details from answers; use the manual contact field")
        for answer in answers:
            q = pending[answer.answer_id]
            source_id = f"answer:{answer.answer_id}"
            ctx["sources"][source_id] = answer.answer
            ctx["answers"][q.field.value] = source_id
            state.graph.nodes.append(GraphNode(id=source_id, kind="assertion", label=answer.answer or "Ответ не предоставлен", source_ids=[source_id], status="user_supplied"))
            if q.node_id:
                state.graph.edges.append(GraphEdge(source=source_id, target=q.node_id, label="ответ пользователя"))
        emit(self, task_id, "human_answers_received", answers=[a.model_dump() for a in answers], graph=state.graph.model_dump())
        state.pending_questions = []
        state.status = "building_card"
        self.jobs[task_id] = asyncio.create_task(self._advance(task_id))
        return state

    async def close(self):
        for handle in self.deadlines.values():
            handle.cancel()
        for job in self.jobs.values():
            if not job.done():
                job.cancel()
        await asyncio.gather(*self.jobs.values(), return_exceptions=True)

    def contract(self):
        from .prompts import contract
        return contract()
