"""Independent bounded LIGHT pipeline; no product persistence or publication."""
import asyncio
import json
import os
import re
import time
from pathlib import Path
from uuid import uuid4
from pydantic import ValidationError
from engines.common.contracts import (StartRequest, RunView, GraphNode, GraphEdge, Question,
    Answer, CardField, CardDraft)
from engines.common.llm import build_role_clients
from engines.common.channel import AnswerChannel
from engines.common.provenance import validate_card
from engines.common.trace import Trace
from .schemas import Analysis, Critique, AssessedCard, Followup

PROMPTS = Path(__file__).parent / "prompts"
OPPOSITE = "Студенческая команда начинает работу, зная только карточку"
OPPOSITE_NEED = "Начать работу без дополнительных вопросов к бизнесу"
FIELDS = ["title","context","need","users","data","constraints","expected_result","success_criteria","contact","interaction_format"]

class Rejected(ValueError):
    def __init__(self, reason, errors):
        super().__init__(str(errors)); self.reason=reason; self.errors=errors


def normalized(text):
    return " ".join(text.casefold().replace("ё","е").replace("«",'"').replace("»",'"').split())


def parse(text, schema):
    text=text.strip()
    if text.startswith("```") and text.endswith("```"):
        text=re.sub(r"^```(?:json)?\s*", "", text)[:-3].strip()
    return schema.model_validate_json(text)


def validate_analysis(proposal, draft):
    errors=[]
    left={e.id for e in proposal.simplest.elements};right={e.id for e in proposal.opposite.elements}
    all_ids=[e.id for e in proposal.simplest.elements+proposal.opposite.elements]+[c.id for c in proposal.contradictions]+[q.id for q in proposal.questions]
    if len(all_ids)!=len(set(all_ids)) or any(not re.fullmatch(r"[ECQ][1-9][0-9]{0,2}",i) for i in all_ids):
        errors.append("Все ID должны быть уникальными короткими E/C/Q-идентификаторами")
    if len(left)<3 or len(right)<3: errors.append("Нужно минимум 3 элемента у каждой стороны")
    for element in proposal.simplest.elements:
        if not normalized(element.quote) or normalized(element.quote) not in normalized(draft):
            errors.append(f"{element.id}: quote отсутствует в черновике")
    if len(proposal.contradictions)<3: errors.append("Нужно минимум 3 противоречия/пробела")
    for c in proposal.contradictions:
        if not c.simplest_refs or not set(c.simplest_refs)<=left: errors.append(f"{c.id}: неверные simplest_refs")
        if not c.opposite_refs or not set(c.opposite_refs)<=right: errors.append(f"{c.id}: неверные opposite_refs")
        if c.field=="contact": errors.append(f"{c.id}: contact только ручное поле")
    if not 3<=len(proposal.questions)<=5: errors.append("Нужно 3–5 вопросов")
    errors.extend(validate_questions(proposal.questions,{c.id:c for c in proposal.contradictions}))
    if {q.contradiction_id for q in proposal.questions}!={c.id for c in proposal.contradictions}:
        errors.append("Каждое противоречие должно быть покрыто вопросом")
    return errors


def validate_questions(questions, contradictions, existing=()):
    errors=[]; ids=[q.id for q in questions]
    if len(ids)!=len(set(ids)) or set(ids)&set(existing): errors.append("ID вопросов повторяются")
    for q in questions:
        if not re.fullmatch(r"Q[1-9][0-9]{0,2}",q.id): errors.append("Нужен короткий Q-id")
        if q.text.count("?")!=1: errors.append(f"{q.id}: нужен ровно один знак ?")
        if q.contradiction_id not in contradictions: errors.append(f"{q.id}: неизвестное противоречие")
        elif q.field != contradictions[q.contradiction_id].field: errors.append(f"{q.id}: поле не совпадает с противоречием")
    return errors


class LightEngine:
    name="light"

    def __init__(self, actor=None, judge=None, *, channel=None, judge_enabled=None, max_rounds=None):
        if actor is None:
            actor, default_judge=build_role_clients()
            judge=judge or default_judge
        self.actor,self.judge=actor,judge
        if actor is judge and judge is not None: raise ValueError("Actor и judge должны быть разными моделями")
        actor_model=getattr(actor,"model",None);judge_model=getattr(judge,"model",None)
        if actor_model and actor_model==judge_model: raise ValueError("Actor и judge должны быть разными моделями")
        self.judge_enabled=(os.getenv("LIGHT_JUDGE","1")=="1") if judge_enabled is None else judge_enabled
        self.max_rounds=min(2,max(1,int(max_rounds or os.getenv("LIGHT_MAX_ROUNDS","2"))))
        self.channel=channel or AnswerChannel(timeout_s=float(os.getenv("ANSWER_TIMEOUT_S","900")))
        self.runs={};self.jobs={};self.contexts={};self.traces={}

    async def start(self, req: StartRequest):
        run_id=uuid4().hex
        self.runs[run_id]=RunView(run_id=run_id,task_id=req.task_id,engine="light",status="analyzing",
            metrics={"llm_calls":0,"tokens_by_role":{},"elapsed_s":0,"rejections":0,"rounds":0,"judge_unavailable":0})
        self.contexts[run_id]={"request":req,"sources":{"draft":req.draft},"answer_fields":{},"observations":[],"active_s":0.0,"started":time.monotonic()}
        self.traces[run_id]=Trace(run_id)
        self._event(run_id,"run_started")
        self.jobs[run_id]=asyncio.create_task(self._run(run_id))
        return run_id

    def view(self, run_id): return self.runs[run_id].model_copy(deep=True)

    def trace(self, run_id): return list(self.traces[run_id].events)

    def sources(self, run_id): return dict(self.contexts[run_id]["sources"])

    async def submit_answers(self, run_id, answers: list[Answer]):
        state=self.runs[run_id]
        if state.status!="waiting_answers": raise ValueError("Прогон не ожидает ответов")
        ids=[a.question_id for a in answers]
        if len(ids)!=len(set(ids)) or set(ids)!={q.question_id for q in state.pending_questions}:
            raise ValueError("Нужен один ответ на каждый текущий вопрос; пустой ответ допустим")
        self.channel.submit(run_id,answers)

    async def cancel(self, run_id):
        self.channel.cancel(run_id)
        self.jobs[run_id].cancel()
        await asyncio.gather(self.jobs[run_id],return_exceptions=True)
        state=self.runs[run_id];state.status="cancelled";state.stop_reason="cancelled";state.pending_questions=[]
        self._event(run_id,"run_finished")

    def _event(self, run_id, event, **fields):
        state=self.runs[run_id];ctx=self.contexts[run_id]
        state.metrics["elapsed_s"]=round(ctx["active_s"],3)
        state.metrics["wall_elapsed_s"]=round(time.monotonic()-ctx["started"],3)
        self.traces[run_id].add(event,**fields,view=state.model_dump())

    async def _call(self, run_id, role, prompt, payload, schema):
        state=self.runs[run_id];ctx=self.contexts[run_id]
        if state.metrics["llm_calls"]>=int(os.getenv("LIGHT_MAX_CALLS","10")): raise Rejected("budget",["Исчерпан бюджет вызовов"])
        remaining=float(os.getenv("LIGHT_DEADLINE_S","45"))-ctx["active_s"]
        if remaining<=0: raise Rejected("deadline",["Исчерпан бюджет времени модели"])
        client=self.actor if role=="actor" else self.judge
        if client is None: raise Rejected("provider_unavailable",["Провайдер роли не настроен"])
        state.metrics["llm_calls"]+=1;started=time.monotonic()
        try:
            async with asyncio.timeout(remaining):
                result=await client.generate([{"role":"system","content":prompt+"\nJSON schema: "+json.dumps(schema.model_json_schema(),ensure_ascii=False)},{"role":"user","content":json.dumps(payload,ensure_ascii=False)}],json_schema=schema.model_json_schema(),max_tokens=5000,timeout=min(remaining,60))
        except Exception as exc:
            self._event(run_id,"llm_call",role=role,usage={},duration_s=time.monotonic()-started,error_type=type(exc).__name__)
            raise
        finally: ctx["active_s"]+=time.monotonic()-started
        if not result.text or not result.text.strip(): raise Rejected("provider_unavailable",["Провайдер вернул пустой ответ"])
        role_usage=state.metrics["tokens_by_role"].setdefault(role,{})
        for key,value in result.usage.items():
            if isinstance(value,int): role_usage[key]=role_usage.get(key,0)+value
        self._event(run_id,"llm_call",role=role,usage=result.usage,duration_s=time.monotonic()-started,output=result.text)
        try: return parse(result.text,schema)
        except (ValueError,ValidationError) as exc: raise Rejected("schema_failed",[str(exc)]) from exc

    async def _proposal(self,run_id,payload):
        prompt=(PROMPTS/"analyze.md").read_text(encoding="utf-8")
        for attempt in range(2):
            try:
                proposal=await self._call(run_id,"actor",prompt,payload,Analysis)
                errors=validate_analysis(proposal,self.contexts[run_id]["request"].draft)
                if errors: raise Rejected("structural_failed",errors)
                self._event(run_id,"proposal",stage="ANALYZE",proposal=proposal.model_dump())
                return proposal
            except Rejected as exc:
                if exc.reason not in {"schema_failed","structural_failed"}: raise
                self.runs[run_id].metrics["rejections"]+=1
                self._event(run_id,"rejected",stage="ANALYZE",reason=exc.reason,errors=exc.errors)
                if attempt: raise
                payload={**payload,"correction":exc.errors}

    def _graph(self,run_id,proposal):
        state=self.runs[run_id]
        state.graph.nodes=[GraphNode(id="P1",kind="simplest",label=proposal.simplest.content),GraphNode(id="P2",kind="opposite",label=OPPOSITE+": "+OPPOSITE_NEED)]
        state.graph.edges=[]
        for side,elements,parent in [("simplest",proposal.simplest.elements,"P1"),("opposite",proposal.opposite.elements,"P2")]:
            for e in elements:
                state.graph.nodes.append(GraphNode(id=e.id,kind="element",label=e.statement,side=side))
                state.graph.edges.append(GraphEdge(source=parent,target=e.id,kind="develops"))
        for c in proposal.contradictions:
            state.graph.nodes.append(GraphNode(id=c.id,kind="contradiction",label=c.statement,status="open"))
            for ref in c.simplest_refs+c.opposite_refs: state.graph.edges.append(GraphEdge(source=ref,target=c.id,kind="contradicts"))
        self._event(run_id,"committed",stage="ANALYZE")

    async def _ask(self,run_id,questions):
        state=self.runs[run_id];ctx=self.contexts[run_id]
        state.pending_questions=[Question(question_id=q.id,text=q.text,field=q.field,contradiction_id=q.contradiction_id,why=q.why,points_at_stake=ctx["request"].field_weights.get(q.field,0)) for q in questions]
        for q in questions:
            state.graph.nodes.append(GraphNode(id=q.id,kind="question",label=q.text))
            state.graph.edges.append(GraphEdge(source=q.id,target=q.contradiction_id,kind="resolves"))
        state.status="waiting_answers";state.metrics["rounds"]+=1
        if "time_to_questions_s" not in state.metrics: state.metrics["time_to_questions_s"]=ctx["active_s"]
        self._event(run_id,"questions_asked",questions=[q.model_dump() for q in state.pending_questions])
        answers=await self.channel.ask(run_id,state.pending_questions)
        qmap={q.question_id:q for q in state.pending_questions}
        received=[]
        for answer in answers:
            aid=f"A{len(ctx['answer_fields'])+1}"
            ctx["sources"][aid]=answer.text;ctx["answer_fields"][aid]=qmap[answer.question_id].field
            ctx["observations"].append({"answer_id":aid,"contradiction_id":qmap[answer.question_id].contradiction_id,"text":answer.text})
            received.append({"source_id":aid,"question_id":answer.question_id,"text":answer.text,"field":qmap[answer.question_id].field})
            state.graph.nodes.append(GraphNode(id=aid,kind="answer",label=answer.text or "Не знаю"))
            state.graph.edges.append(GraphEdge(source=aid,target=answer.question_id,kind="answers"))
        state.pending_questions=[];state.status="building_card"
        self._event(run_id,"answers_received",answers=received)

    async def _card(self,run_id,proposal):
        ctx=self.contexts[run_id];state=self.runs[run_id]
        payload={"draft":ctx["request"].draft,"analysis":proposal.model_dump(),"sources":ctx["sources"],"answer_fields":ctx["answer_fields"],"observations":ctx["observations"]}
        result=None;broken=None
        for attempt in range(3):
            previous=result
            try:
                result=await self._call(run_id,"actor",(PROMPTS/"card.md").read_text(encoding="utf-8"),payload,AssessedCard)
                ids=[a.contradiction_id for a in result.assessment]
                if set(ids)!={c.id for c in proposal.contradictions} or len(ids)!=len(set(ids)):
                    raise Rejected("structural_failed",["Оцените каждое существующее противоречие ровно один раз"])
                for assessment in result.assessment:
                    observed=[o["text"] for o in ctx["observations"] if o["contradiction_id"]==assessment.contradiction_id]
                    if assessment.relation=="resolved" and observed and all(normalized(t).strip(".!?") in {"","не знаю","неизвестно"} for t in observed):
                        raise Rejected("structural_failed",[f"{assessment.contradiction_id}: неизвестный/пустой ответ не подтверждает разрешение"])
                if previous is not None and broken is not None:
                    for name,field in previous.card.fields.items():
                        if name not in broken: result.card.fields[name]=field
                errors=validate_card(result.card,ctx["sources"])
                for name,field in result.card.fields.items():
                    if field.value and normalized(field.value)!=normalized(" ".join(c.quote for c in field.sources)):
                        errors.append({"field":name,"code":"nonextractive_value","message":"Значение должно дословно состоять из цитат, соединённых пробелом"})
                    if name=="contact" and field.value: errors.append({"field":name,"code":"manual_contact","message":"Контакт заполняет человек"})
                    for citation in field.sources:
                        if citation.source_id in ctx["answer_fields"] and ctx["answer_fields"][citation.source_id]!=name:
                            errors.append({"field":name,"code":"wrong_answer_field","message":"Ответ относится к другому полю"})
                self._event(run_id,"card_validated",attempt=attempt+1,errors=errors)
                if not errors: return result
                broken={e["field"] for e in errors}
                if attempt==2:
                    for name in broken: result.card.fields[name]=CardField(value=None,sources=[])
                    if validate_card(result.card,ctx["sources"]): raise Rejected("provenance_failed",errors)
                    self._event(run_id,"committed",stage="NULL_UNSUPPORTED_FIELDS",fields=sorted(broken))
                    return result
                payload={**payload,"card":result.card.model_dump(),"correction":errors}
                state.metrics["rejections"]+=1;self._event(run_id,"rejected",stage="CARD",reason="provenance_failed",errors=errors)
            except Rejected as exc:
                if exc.reason not in {"schema_failed","structural_failed"} or attempt>=1: raise
                state.metrics["rejections"]+=1;self._event(run_id,"rejected",stage="CARD",reason=exc.reason,errors=exc.errors)
                payload={**payload,"correction":exc.errors}
        raise Rejected("provenance_failed",["Карточка не проверена"])

    async def _run(self,run_id):
        state=self.runs[run_id];ctx=self.contexts[run_id]
        try:
            proposal=await self._proposal(run_id,{"draft":ctx["request"].draft,"opposite":OPPOSITE,"opposite_need":OPPOSITE_NEED})
            if self.judge_enabled:
                try:
                    critique=await self._call(run_id,"judge",(PROMPTS/"critique.md").read_text(encoding="utf-8"),{"draft":ctx["request"].draft,"analysis":proposal.model_dump()},Critique)
                except Exception as exc:
                    if isinstance(exc,Rejected) and exc.reason in {"budget","deadline"}: raise
                    state.metrics["judge_unavailable"]+=1
                    self._event(run_id,"rejected",stage="CRITIQUE",reason="judge_unavailable")
                else:
                    if not critique.accepted:
                        state.metrics["rejections"]+=1
                        self._event(run_id,"rejected",stage="CRITIQUE",reason="semantic",issues=[i.model_dump() for i in critique.issues])
                        proposal=await self._call(run_id,"actor",(PROMPTS/"analyze.md").read_text(encoding="utf-8"),{"draft":ctx["request"].draft,"previous":proposal.model_dump(),"correction":[i.model_dump() for i in critique.issues]},Analysis)
                        errors=validate_analysis(proposal,ctx["request"].draft)
                        if errors: raise Rejected("structural_failed",errors)
            self._graph(run_id,proposal)
            questions=proposal.questions
            for round_index in range(self.max_rounds):
                await self._ask(run_id,questions)
                result=await self._card(run_id,proposal)
                relations={a.contradiction_id:a.relation for a in result.assessment}
                for node in state.graph.nodes:
                    if node.id in relations: node.status={"resolved":"resolved","not_resolved":"open","new_gap":"reopened"}[relations[node.id]]
                gaps={c.id:c for c in proposal.contradictions if relations[c.id]=="new_gap" and ctx["request"].field_weights.get(c.field,0)>=10}
                if gaps and round_index+1<self.max_rounds:
                    follow=await self._call(run_id,"actor",(PROMPTS/"followup.md").read_text(encoding="utf-8"),{"gaps":[c.model_dump() for c in gaps.values()],"assessment":[a.model_dump() for a in result.assessment],"sources":ctx["sources"],"used_question_ids":[n.id for n in state.graph.nodes if n.kind=="question"]},Followup)
                    errors=validate_questions(follow.questions,gaps,[n.id for n in state.graph.nodes])
                    if errors: raise Rejected("structural_failed",errors)
                    questions=follow.questions
                else: break
            for name in FIELDS: result.card.fields.setdefault(name,CardField(value=None))
            state.card=result.card
            for name,field in result.card.fields.items():
                if field.value is not None:
                    fid=f"F{FIELDS.index(name)+1}";state.graph.nodes.append(GraphNode(id=fid,kind="field",label=field.value,status="ok"))
                    for source in field.sources:
                        state.graph.edges.append(GraphEdge(source="P1" if source.source_id=="draft" else source.source_id,target=fid,kind="grounds"))
            state.status="card_ready";state.stop_reason="human_confirmation_required"
        except asyncio.CancelledError:
            state.status="cancelled";state.stop_reason="cancelled"
        except TimeoutError:
            reason="answer_timeout" if state.status=="waiting_answers" else "deadline"
            state.status="failed";state.stop_reason=reason
            state.error="Истекло время ожидания ответа или анализа"
        except Exception as exc:
            state.status="failed";state.stop_reason=exc.reason if isinstance(exc,Rejected) else "provider_unavailable"
            state.error="Анализ остановлен: " + ("; ".join(map(str,exc.errors)) if isinstance(exc,Rejected) else type(exc).__name__)
        finally:
            state.pending_questions=[]
            self._event(run_id,"run_finished")

    def contract(self):
        return {"engine":"light","prompts":{p.stem:p.read_text(encoding="utf-8") for p in PROMPTS.glob("*.md")},
            "schemas":{s.__name__:s.model_json_schema() for s in [Analysis,Critique,AssessedCard,Followup]},
            "invalid_response_examples":self._invalid_examples()}

    def _invalid_examples(self):
        examples=[]
        for trace in self.traces.values():
            output=None
            for event in trace.events:
                if event.get("event")=="llm_call": output=event.get("output")
                if event.get("event")=="rejected":
                    examples.append({"actual_output":output,"rejection":{k:v for k,v in event.items() if k!="view"},"source":"actual run trace"})
        return examples[:5]
