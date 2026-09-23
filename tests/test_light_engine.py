"""Offline scripted LIGHT conformance; no production provider calls."""
import asyncio
import copy
import json
import unittest
from engines.light import LightEngine
from engines.light.schemas import Analysis
from engines.light.engine import validate_analysis
from engines.common.llm import ScriptedLLM, ProviderError
from engines.common.channel import AnswerChannel
from engines.common.contracts import StartRequest, Answer
from engines.common.provenance import validate_card

DRAFT="Нужен помощник клиентам. Данных пока нет. Срок месяц."
WEIGHTS={"data":20,"users":10,"success_criteria":15}

def analysis_fixture():
    return {"simplest":{"content":"Потребность бизнеса", "elements":[
        {"id":"E1","dimension":"need","statement":"Нужен помощник","quote":"Нужен помощник клиентам."},
        {"id":"E2","dimension":"data","statement":"Данных нет","quote":"Данных пока нет."},
        {"id":"E3","dimension":"constraints","statement":"Срок месяц","quote":"Срок месяц."}]},
        "opposite":{"content":"Команда начинает работу","need":"Начать без вопросов", "elements":[
        {"id":"E4","dimension":"data","statement":"Нужны данные"},
        {"id":"E5","dimension":"users","statement":"Нужны роли пользователей"},
        {"id":"E6","dimension":"success_criteria","statement":"Нужен критерий приёмки"}]},
        "contradictions":[{"id":f"C{i}","kind":"gap","field":field,"simplest_refs":[f"E{i}"],"opposite_refs":[f"E{i+3}"],"statement":"Неизвестное условие старта"} for i,field in enumerate(WEIGHTS,1)],
        "questions":[{"id":f"Q{i}","text":f"Уточните {field}?","field":field,"contradiction_id":f"C{i}","why":"Нужно для старта"} for i,field in enumerate(WEIGHTS,1)]}

def card_fixture(value="Доступен CSV",relation="resolved"):
    return {"assessment":[{"contradiction_id":f"C{i}","relation":relation if i==1 else "not_resolved","explanation":"По ответу бизнеса"} for i in range(1,4)],
            "card":{"fields":{"data":{"value":value,"sources":[{"source_id":"A1","quote":"Доступен CSV"}]}}}}

async def wait_status(engine,rid,status):
    for _ in range(1000):
        state=engine.view(rid)
        if state.status==status:return state
        if state.status in {"failed","cancelled","card_ready"}:raise AssertionError(state.model_dump())
        await asyncio.sleep(0.001)
    raise AssertionError("Timeout")

class LightTests(unittest.IsolatedAsyncioTestCase):
    def make_engine(self, outputs=None, judge=None, **kwargs):
        actor=ScriptedLLM([json.dumps(x,ensure_ascii=False) if isinstance(x,dict) else x for x in (outputs or [analysis_fixture(),card_fixture()])])
        judge=judge or ScriptedLLM(['{"accepted":true,"issues":[]}'])
        return LightEngine(actor=actor,judge=judge,max_rounds=1,**kwargs)
    async def answer(self,engine,rid):
        state=await wait_status(engine,rid,"waiting_answers")
        await engine.submit_answers(rid,[Answer(question_id=q.question_id,text="Доступен CSV" if q.field=="data" else "Не знаю") for q in state.pending_questions])
    async def test_happy_path_graph_provenance_and_view_no_calls(self):
        engine=self.make_engine();rid=await engine.start(StartRequest(task_id="t",draft=DRAFT,field_weights=WEIGHTS))
        state=await wait_status(engine,rid,"waiting_answers")
        self.assertGreaterEqual(len(state.pending_questions),3)
        for q in state.pending_questions:
            self.assertIn(q.contradiction_id,{n.id for n in state.graph.nodes})
            self.assertEqual(q.points_at_stake,WEIGHTS[q.field])
        before=len(engine.actor.calls);engine.view(rid);self.assertEqual(len(engine.actor.calls),before)
        await self.answer(engine,rid);state=await wait_status(engine,rid,"card_ready")
        self.assertEqual(validate_card(state.card,engine.contexts[rid]["sources"]),[])
        self.assertIsNone(state.card.fields["constraints"].value)
        self.assertEqual(state.metrics["llm_calls"],3)
        self.assertIn("card_validated",[e["event"] for e in engine.trace(rid)])
    async def test_broken_json_repaired(self):
        engine=self.make_engine(["broken json",analysis_fixture(),card_fixture()]);rid=await engine.start(StartRequest(task_id="t",draft=DRAFT,field_weights=WEIGHTS))
        await self.answer(engine,rid);state=await wait_status(engine,rid,"card_ready")
        self.assertEqual(state.metrics["rejections"],1)
    async def test_repeated_bad_analysis_fails_honestly(self):
        engine=self.make_engine(["bad","bad"]);rid=await engine.start(StartRequest(task_id="t",draft=DRAFT,field_weights=WEIGHTS))
        await engine.jobs[rid];self.assertEqual(engine.view(rid).status,"failed")
        self.assertEqual(engine.view(rid).stop_reason,"schema_failed")
    async def test_hallucinated_number_null_after_two_repairs(self):
        engine=self.make_engine([analysis_fixture(),card_fixture("Доступен CSV 999"),card_fixture("Доступен CSV 999"),card_fixture("Доступен CSV 999")])
        rid=await engine.start(StartRequest(task_id="t",draft=DRAFT,field_weights=WEIGHTS));await self.answer(engine,rid)
        state=await wait_status(engine,rid,"card_ready")
        self.assertIsNone(state.card.fields["data"].value)
    async def test_judge_unavailable_does_not_poison_actor(self):
        engine=self.make_engine(judge=ScriptedLLM([ProviderError("network")]))
        rid=await engine.start(StartRequest(task_id="t",draft=DRAFT,field_weights=WEIGHTS));await self.answer(engine,rid)
        state=await wait_status(engine,rid,"card_ready");self.assertEqual(state.metrics["judge_unavailable"],1)
        self.assertNotIn("judge_unavailable",json.dumps(engine.actor.calls))
    async def test_answer_timeout_preserves_graph(self):
        engine=self.make_engine(channel=AnswerChannel(timeout_s=.01));rid=await engine.start(StartRequest(task_id="t",draft=DRAFT,field_weights=WEIGHTS));await engine.jobs[rid]
        state=engine.view(rid);self.assertEqual(state.stop_reason,"answer_timeout");self.assertTrue(state.graph.nodes)
    async def test_parallel_runs_and_cancel(self):
        engine=self.make_engine([analysis_fixture(),analysis_fixture()],judge=ScriptedLLM(['{"accepted":true,"issues":[]}']*2))
        a=await engine.start(StartRequest(task_id="a",draft=DRAFT,field_weights=WEIGHTS));b=await engine.start(StartRequest(task_id="b",draft=DRAFT,field_weights=WEIGHTS))
        await wait_status(engine,a,"waiting_answers");await wait_status(engine,b,"waiting_answers")
        await engine.cancel(a);self.assertEqual(engine.view(a).status,"cancelled");self.assertEqual(engine.view(b).status,"waiting_answers");await engine.cancel(b)
    async def test_second_round_reassesses_new_gap(self):
        follow={"questions":[{"id":"Q4","text":"Какие данные доступны сейчас?","field":"data","contradiction_id":"C1","why":"Уточнение нового пробела"}]}
        actor=ScriptedLLM([json.dumps(x,ensure_ascii=False) for x in [analysis_fixture(),card_fixture(relation="new_gap"),follow,card_fixture()]])
        engine=LightEngine(actor=actor,judge=ScriptedLLM(['{"accepted":true,"issues":[]}']),max_rounds=2)
        rid=await engine.start(StartRequest(task_id="t",draft=DRAFT,field_weights=WEIGHTS));await self.answer(engine,rid)
        for _ in range(100):
            await asyncio.sleep(.001)
            if engine.view(rid).status=="waiting_answers" and engine.view(rid).pending_questions[0].question_id=="Q4":break
        await engine.submit_answers(rid,[Answer(question_id="Q4",text="Доступен CSV")])
        state=await wait_status(engine,rid,"card_ready")
        self.assertEqual(state.metrics["rounds"],2)
        self.assertEqual(state.metrics["llm_calls"],5)
    async def test_judge_rejection_has_one_actor_correction(self):
        judge=ScriptedLLM(['{"accepted":false,"issues":[{"target_id":"Q1","problem":"Уточните вопрос"}]}'])
        engine=self.make_engine([analysis_fixture(),analysis_fixture(),card_fixture()],judge=judge)
        rid=await engine.start(StartRequest(task_id="t",draft=DRAFT,field_weights=WEIGHTS));await self.answer(engine,rid)
        state=await wait_status(engine,rid,"card_ready");self.assertEqual(state.metrics["rejections"],1)
        self.assertEqual(len(judge.calls),1)
    async def test_unknown_answer_cannot_resolve_contradiction(self):
        invalid=card_fixture();invalid["assessment"][1]["relation"]="resolved"
        engine=self.make_engine([analysis_fixture(),invalid,card_fixture()])
        rid=await engine.start(StartRequest(task_id="t",draft=DRAFT,field_weights=WEIGHTS));await self.answer(engine,rid)
        state=await wait_status(engine,rid,"card_ready");self.assertEqual(state.metrics["rejections"],1)

    def test_structural_cross_side_refs_and_question_coverage(self):
        proposal=analysis_fixture();proposal["contradictions"][0]["opposite_refs"]=["E1"]
        self.assertTrue(validate_analysis(Analysis.model_validate(proposal),DRAFT))
        proposal=analysis_fixture();proposal["questions"][0]["text"]="Два? Вопроса?"
        self.assertTrue(validate_analysis(Analysis.model_validate(proposal),DRAFT))

if __name__=="__main__": unittest.main()
