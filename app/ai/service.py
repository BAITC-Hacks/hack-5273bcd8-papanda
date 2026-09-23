"""Bounded task runs: the light engine or the explicit deterministic stub, behind a strict source gate."""
import asyncio
import os
import time
from uuid import uuid4
from app.contracts import (Answer, Card, CardField, FieldName, FIELD_WEIGHTS,
    GraphNode, GraphEdge, Question, RunState, Source)
from .validation import validate_card, contains_contact, validate_assignments, is_unknown_answer
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
        if mode == "engine":  # configuration alias for the light engine
            mode = "light"
        if mode not in {"stub", "light"}:
            raise ValueError("Unsupported AI mode")
        contact = task.card.fields[FieldName.contact].value
        if contains_contact(task.text, contact):
            raise ValueError("Remove contact details from description; use the manual contact field")
        # Whitelist live provenance only. Historical edit_UUID sources may be old contacts.
        forbidden = {s.source_id for s in task.card.fields[FieldName.contact].sources}
        current_refs = {s.source_id for name, field in task.card.fields.items()
                        if name != FieldName.contact for s in field.sources}
        sources = {k: v for k, v in task.sources.items()
                   if (k.startswith(("answer:", "prior:")) or (k.startswith("A") and k[1:].isdigit()) or k in current_refs) and k not in forbidden
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
        self.jobs[task_id] = asyncio.create_task(
            self._advance_external(task_id) if mode == "light" else self._advance(task_id))
        return state

    async def _advance(self, task_id):
        state, ctx = self.runs[task_id], self.contexts[task_id]
        try:
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

    async def _advance_external(self, task_id):
        """Translate the light engine contract into the existing product API."""
        state, ctx = self.runs[task_id], self.contexts[task_id]
        engine = None
        try:
            # Imported inside try: any failure must end the run visibly, not leave it "analyzing".
            from engines import get_engine
            from engines.common.contracts import StartRequest
            engine = get_engine(state.mode)
            task = self.store.get_task(task_id)
            # Keep user provenance separate from the new run's A1/A2 answers.
            prior = {(key if key.startswith(("field:", "prior:")) else f"prior:{task.revision}:{key}"): value
                     for key, value in ctx["sources"].items() if key != "draft"}
            ctx["sources"] = {"draft": task.text, **prior}
            engine_id = await engine.start(StartRequest(
                task_id=task_id, draft=task.text, industry=task.industry,
                field_weights=FIELD_WEIGHTS, prior_sources=prior,
            ))
            ctx["engine_id"] = engine_id
            ctx["engine"] = engine
            while True:
                view = engine.view(engine_id)
                self._copy_external_view(state, view)
                if view.status == "card_ready":
                    card = Card()
                    for name, field in (view.card.fields if view.card else {}).items():
                        if field.value:
                            card.fields[FieldName(name)] = CardField(
                                value=field.value, status="ai_proposed",
                                sources=[Source(source_id=s.source_id, quote=s.quote) for s in field.sources],
                            )
                    # Engine formulations are re-checked here against their cited quotes.
                    self._commit(task_id, card, formulation=True)
                    break
                if view.status in {"failed", "cancelled"}:
                    state.status = "error"
                    state.error = view.error or "Движок остановлен; карточка не создана"
                    state.stop_reason = view.stop_reason or view.status
                    break
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            if engine is not None and ctx.get("engine_id"):
                await engine.cancel(ctx["engine_id"])
            raise
        except Exception as exc:
            state.status = "error"
            state.stop_reason = "engine_bridge_error"
            state.error = f"Движок остановлен ({type(exc).__name__}); карточка не создана"
            emit(self, task_id, "engine_bridge_error", exception_type=type(exc).__name__)
        finally:
            state.elapsed_seconds = round(time.monotonic() - ctx["started"], 3)
            emit(self, task_id, "state_transition", state=state.model_dump())

    @staticmethod
    def _copy_external_view(state, view):
        from app.contracts import Graph
        state.status = "error" if view.status in {"failed", "cancelled"} else view.status
        state.pending_questions = [Question(
            answer_id=q.question_id, field=FieldName(q.field), question=q.text,
            why=q.why, points_at_stake=q.points_at_stake, node_id=q.contradiction_id,
        ) for q in view.pending_questions]
        state.graph = Graph(
            # Elements carry their side; keep it visible as business fact vs team need.
            nodes=[GraphNode(id=n.id, kind={"simplest": "business_fact", "opposite": "team_need"}.get(n.side, n.kind)
                             if n.kind == "element" else n.kind, label=n.label,
                             status=n.status or "proposed") for n in view.graph.nodes],
            edges=[GraphEdge(source=e.source, target=e.target, label=e.kind) for e in view.graph.edges],
        )
        state.calls = int(view.metrics.get("llm_calls", 0))
        state.tokens = {role: (usage["total_tokens"] if isinstance(usage.get("total_tokens"), int)
                              else sum(usage.get(key, 0) for key in ("prompt_tokens", "completion_tokens")
                                       if isinstance(usage.get(key, 0), int)))
                        for role, usage in view.metrics.get("tokens_by_role", {}).items()
                        if isinstance(usage, dict)}
        state.elapsed_seconds = float(view.metrics.get("elapsed_s", 0))

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
                if not is_unknown_answer(value):
                    card.fields[FieldName(field)] = CardField(value=value, status="ai_proposed", sources=[Source(source_id=source_id, quote=value)])
            self._commit(task_id, card)

    def _commit(self, task_id, card, formulation=False):
        state, ctx = self.runs[task_id], self.contexts[task_id]
        self._expire(task_id)
        if state.status == "error":
            raise ValueError("Run expired before commit")
        validate_card(card, ctx["sources"], formulation)
        validate_assignments(card, ctx["answers"])
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
        if state.mode == "light":
            from engines.common.contracts import Answer as EngineAnswer
            engine = ctx.get("engine")
            engine_id = ctx.get("engine_id")
            if engine is None or engine_id is None:
                raise ValueError("Движок ещё не готов принять ответы")
            converted = []
            for answer in answers:
                q = pending[answer.answer_id]
                index = ctx.setdefault("external_answer_index", 0) + 1
                ctx["external_answer_index"] = index
                source_id = f"A{index}"
                ctx["sources"][source_id] = answer.answer
                if not is_unknown_answer(answer.answer):
                    ctx["answers"][q.field.value] = source_id
                converted.append(EngineAnswer(question_id=answer.answer_id, text=answer.answer))
            await engine.submit_answers(engine_id, converted)
            self._copy_external_view(state, engine.view(engine_id))
            return state
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

