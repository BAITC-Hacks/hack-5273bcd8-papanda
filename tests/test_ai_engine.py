import asyncio
import json
import os
import unittest
from unittest.mock import patch
import httpx
from app.ai import engine
from app.ai.service import TaskRunService
from app.contracts import Answer, Card, CardField, FieldName, Source
from test_ai_service import Store

ENV={"OPENAI_API_KEY":"test-secret-never-sent", "AI_ACTOR_MODEL":"actor-model", "AI_JUDGE_MODEL":"judge-model"}


def make_world():
    return engine.World(assertions=[engine.Assertion(id="a", value="Нужен отчёт по продажам", sources=[Source(source_id="draft", quote="Нужен отчёт по продажам")])],
        business_need="Гипотеза: уточнить потребность в отчёте", student_prerequisites="Гипотеза: команде нужны данные и условия", candidates=[
        engine.Candidate(id=k, kind="gap", field=k, interpretation=f"Не уточнено {k}", evidence=["a"]) for k in ["data","users","constraints"]])

class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_world_question_revision_commit_with_mocked_models(self):
        service=TaskRunService(Store())
        worlds=[]
        async def fake_call(s, tid, role, instruction, schema, payload):
            s.runs[tid].calls+=1
            if schema is engine.World:
                worlds.append(payload)
                return make_world()
            if schema is engine.Judgment: return engine.Judgment(accepted=["data","users","constraints"], explanation="grounded gaps")
            if schema is engine.Questions: return engine.Questions(questions=[engine.Ask(candidate_id=k, question=f"Уточните {k}?") for k in ["data","users","constraints"]])
            if schema is Card:
                card=Card(); card.fields[FieldName.context]=CardField(value=payload["sources"]["draft"], sources=[Source(source_id="draft",quote=payload["sources"]["draft"])])
                return card
            return engine.CardJudgment(accepted=True,explanation="source faithful")
        with patch.dict(os.environ,ENV), patch.object(engine,"call",fake_call):
            service.start("t","engine"); await service.jobs["t"]
            self.assertEqual(service.get("t").status,"waiting_answers")
            await service.submit_answers("t",[Answer(answer_id=q.answer_id,answer="Уточнение бизнеса") for q in service.get("t").pending_questions]); await service.jobs["t"]
            self.assertEqual(service.get("t").status,"card_ready")
            self.assertIsNotNone(worlds[1]["previous_world"])
            self.assertEqual(len(worlds[1]["sources"]),4)
            self.assertTrue(any(n.id.startswith("r1:") for n in service.get("t").graph.nodes))
            self.assertEqual(service.get("t").calls,7)
        await service.close()

    async def test_malformed_response_is_error_no_fake_card(self):
        class Client:
            def __init__(self,**kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            async def post(self,*args,**kwargs):
                return httpx.Response(200,json={"choices":[{"message":{"content":"not json"}}]},request=httpx.Request("POST","https://example.test"))
        service=TaskRunService(Store())
        with patch.dict(os.environ,ENV),patch.object(engine.httpx,"AsyncClient",Client):
            service.start("t","engine"); await service.jobs["t"]
        self.assertEqual(service.get("t").status,"error")
        self.assertEqual(service.get("t").stop_reason,"schema_error")
        self.assertEqual(service.get("t").calls,2)
        self.assertIsNone(service.get("t").card_draft)
        self.assertNotIn("test-secret",service.get("t").error)
        await service.close()

    async def test_single_card_source_correction_uses_actual_failure(self):
        service=TaskRunService(Store())
        attempts=[]
        async def fake_call(s, tid, role, instruction, schema, payload):
            s.runs[tid].calls+=1
            if schema is engine.World: return make_world()
            if schema is engine.Judgment: return engine.Judgment(accepted=["data","users","constraints"],explanation="gaps")
            if schema is engine.Questions: return engine.Questions(questions=[engine.Ask(candidate_id=k,question=f"Уточните {k}?") for k in ["data","users","constraints"]])
            if schema is Card:
                attempts.append(payload)
                card=Card(); quote=payload["sources"]["draft"]
                card.fields[FieldName.context]=CardField(value=quote if len(attempts)>1 else quote+" 2025",sources=[Source(source_id="draft",quote=quote)])
                return card
            return engine.CardJudgment(accepted=True,explanation="valid")
        with patch.dict(os.environ,ENV),patch.object(engine,"call",fake_call):
            service.start("t","engine");await service.jobs["t"]
            await service.submit_answers("t",[Answer(answer_id=q.answer_id,answer="Ответ") for q in service.get("t").pending_questions]);await service.jobs["t"]
            self.assertEqual(service.get("t").status,"card_ready")
            self.assertEqual(len(attempts),2)
            self.assertIn("Value must equal",attempts[1]["correction"]["failure"])
            self.assertEqual(service.contexts["t"]["corrections"],1)
        await service.close()

    async def test_semantic_rejection_correction_is_bounded(self):
        from app.ai.events import SemanticRejection
        service=TaskRunService(Store());service.start("t","stub");await service.jobs["t"]
        engine.reserve_correction(service,"t",{"kind":"semantic","failure":"wrong field"})
        with self.assertRaises(SemanticRejection):
            engine.reserve_correction(service,"t",{"kind":"semantic","failure":"still wrong"})
        await service.close()

    async def test_call_budget_enforced_before_http(self):
        service=TaskRunService(Store()); service.start("t","stub"); await service.jobs["t"]
        service.get("t").calls=12
        with patch.dict(os.environ,ENV):
            with self.assertRaisesRegex(ValueError,"budget"):
                await engine.call(service,"t","actor","test",engine.World,{})
        await service.close()

    def test_invented_assertion_and_forced_contradiction_rejected(self):
        world=make_world(); world.assertions[0].value+=" за 2025 год"
        with self.assertRaises(ValueError): engine.validate_world(world,{"draft":"Нужен отчёт по продажам"})
        world=make_world(); world.candidates[0].kind="contradiction"
        with self.assertRaises(ValueError): engine.validate_world(world,{"draft":"Нужен отчёт по продажам"})

if __name__ == "__main__": unittest.main()
