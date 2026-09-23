import asyncio
import unittest
import os
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from app.ai.service import TaskRunService
from app.ai.validation import validate_field
from app.contracts import Task, CardField, Source, Answer

class Store:
    def __init__(self):
        self.task = Task(id="t", text="Нужен отчёт по продажам", industry="retail", created_at="now")
    def get_task(self, task_id):
        if task_id != "t": raise KeyError(task_id)
        return self.task.model_copy(deep=True)
    def save_task(self, task): self.task = task.model_copy(deep=True)

class GroundingTests(unittest.TestCase):
    def test_empty_and_mismatched_citations(self):
        for refs in [[], [Source(source_id="d", quote="")], [Source(source_id="x", quote="abc")], [Source(source_id="d", quote="wrong")]]:
            with self.assertRaises(ValueError): validate_field(CardField(value="abc", sources=refs), {"d":"abc"})
    def test_invention_despite_valid_quote(self):
        with self.assertRaises(ValueError):
            validate_field(CardField(value="abc 2025", sources=[Source(source_id="d", quote="abc")]), {"d":"abc"})
    def test_negation_fragment_rejected_complete_sentence_allowed(self):
        source="У нас не есть данные, а только предположение. Нужен отчёт."
        with self.assertRaisesRegex(ValueError,"complete"):
            validate_field(CardField(value="есть данные",sources=[Source(source_id="draft",quote="есть данные")]),{"draft":source})
        validate_field(CardField(value="Нужен отчёт.",sources=[Source(source_id="draft",quote="Нужен отчёт.")]),{"draft":source})

    def test_contract_executes_validation(self):
        self.assertTrue(TaskRunService(Store()).contract()["validation"]["accepted_passed"])

class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self): self.store=Store(); self.service=TaskRunService(self.store)
    async def asyncTearDown(self): await self.service.close()
    async def advance(self): await self.service.jobs["t"]
    async def test_two_rounds_source_grounding_and_no_confirmation(self):
        self.service.start("t", "stub"); await self.advance()
        self.assertGreaterEqual(len(self.service.get("t").pending_questions), 3)
        old=[]
        for i in range(2):
            questions=self.service.get("t").pending_questions
            old=[Answer(answer_id=q.answer_id, answer="Ответ бизнеса") for q in questions]
            await self.service.submit_answers("t", old); await self.advance()
        state=self.service.get("t")
        self.assertEqual(state.status,"card_ready")
        self.assertEqual(state.calls,0)
        self.assertFalse(any(f.status=="confirmed" for f in state.card_draft.fields.values()))
        with self.assertRaises(ValueError): await self.service.submit_answers("t",old)
    async def test_ai_changes_unpublish_but_preserve_manual_fields(self):
        from app.contracts import FieldName
        self.store.task.status="published"
        self.store.task.published_at="now"
        self.store.task.card.fields[FieldName.contact]=CardField(value="private@example.test",status="confirmed")
        self.store.task.card.fields[FieldName.title]=CardField(value="Мой заголовок",status="edited")
        self.service.start("t","stub"); await self.advance()
        self.assertNotIn("private@example.test",str(self.service.contexts["t"]["sources"]))
        while self.service.get("t").status=="waiting_answers":
            self.assertGreaterEqual(len(self.service.get("t").pending_questions),3)
            await self.service.submit_answers("t",[Answer(answer_id=q.answer_id,answer="Новые сведения") for q in self.service.get("t").pending_questions]); await self.advance()
        self.assertEqual(self.store.task.status,"draft")
        self.assertEqual(self.store.task.card.fields[FieldName.contact].value,"private@example.test")
        self.assertEqual(self.store.task.card.fields[FieldName.title].value,"Мой заголовок")

    async def test_human_wait_expires_without_polling(self):
        with patch.dict(os.environ,{"AI_RUN_TIMEOUT":"0.02"}):
            self.service.start("t","stub"); await self.advance()
            self.assertEqual(self.service.get("t").status,"waiting_answers")
            await asyncio.sleep(0.08)
            self.assertEqual(self.service.runs["t"].stop_reason,"run_timeout")
            self.assertEqual(self.service.runs["t"].status,"error")

    async def test_explicit_answer_revises_confirmed_field(self):
        from app.contracts import Card, FieldName
        self.store.task.card.fields[FieldName.data]=CardField(value="Данных нет",status="confirmed")
        self.service.start("t","stub");await self.advance()
        ctx=self.service.contexts["t"]
        ctx["sources"]["new_answer"]="Данные появились"
        ctx["answers"]["data"]="new_answer"
        card=Card();card.fields[FieldName.data]=CardField(value="Данные появились",sources=[Source(source_id="new_answer",quote="Данные появились")])
        self.service._commit("t",card)
        self.assertEqual(self.store.task.card.fields[FieldName.data].value,"Данные появились")
        self.assertEqual(self.store.task.card.fields[FieldName.data].status,"ai_proposed")

    async def test_trace_has_actual_state_and_redacts_credentials(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{"AI_TRACE_DIR":directory,"OPENAI_API_KEY":"sentinel-secret"}):
            self.store.task.text="Описание sentinel-secret"
            state=self.service.start("t","stub");await self.advance()
            content=(Path(directory)/f"{state.run_id}.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("sentinel-secret",content)
            events=[json.loads(line) for line in content.splitlines()]
            self.assertEqual(events[0]["event"],"run_started")
            self.assertEqual(events[-1]["state"]["status"],"waiting_answers")

    async def test_obsolete_contact_source_is_not_in_model_payload(self):
        from app.contracts import FieldName
        self.store.task.sources={"edit_random_old":"old@example.com","edit_noncontact_current":"Проверенные сведения","answer:old":"Сведения ответа"}
        self.store.task.card.fields[FieldName.context]=CardField(value="Проверенные сведения",status="confirmed",sources=[Source(source_id="edit_noncontact_current",quote="Проверенные сведения")])
        self.service.start("t","stub");await self.advance()
        sources=self.service.contexts["t"]["sources"]
        self.assertNotIn("edit_random_old",sources)
        self.assertIn("edit_noncontact_current",sources)
        self.assertIn("answer:old",sources)

    async def test_identical_answer_downgrade_unpublishes(self):
        from app.contracts import Card, FieldName
        self.store.task.status="published";self.store.task.published_at="now"
        self.store.task.card.fields[FieldName.data]=CardField(value="История продаж",status="confirmed")
        self.service.start("t","stub");await self.advance()
        ctx=self.service.contexts["t"];ctx["sources"]["answer:new"]="История продаж";ctx["answers"]["data"]="answer:new"
        card=Card();card.fields[FieldName.data]=CardField(value="История продаж",sources=[Source(source_id="answer:new",quote="История продаж")])
        self.service._commit("t",card)
        self.assertEqual(self.store.task.status,"draft")
        self.assertIsNone(self.store.task.published_at)
        self.assertEqual(self.store.task.card.fields[FieldName.data].status,"ai_proposed")

    async def test_contact_in_free_text_rejected_before_run(self):
        self.store.task.text="Нужен отчёт. Пишите old@example.com"
        with self.assertRaisesRegex(ValueError,"contact"):
            self.service.start("t","engine")
        self.assertNotIn("t",self.service.runs)

    async def test_stale_question_ids(self):
        self.service.start("t", "stub"); await self.advance()
        with self.assertRaises(ValueError): await self.service.submit_answers("t", [Answer(answer_id="stale", answer="x")])
    async def test_changed_revision_rejects_answers(self):
        self.service.start("t", "stub"); await self.advance()
        self.store.task.revision+=1
        with self.assertRaises(ValueError):
            await self.service.submit_answers("t", [Answer(answer_id=q.answer_id, answer="x") for q in self.service.get("t").pending_questions])
    async def test_no_engine_configuration_does_not_fake_success(self):
        self.service.start("t", "engine"); await self.advance()
        self.assertEqual(self.service.get("t").status,"error")

if __name__ == "__main__": unittest.main()
