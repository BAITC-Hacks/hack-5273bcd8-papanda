import asyncio
import pytest

from engines.common.channel import AnswerChannel
from engines.common.contracts import Answer, CardDraft, CardField, Source
from engines.common.json_output import extract_json, parse_or_repair
from engines.common.llm import LLMResult, ProviderError, ScriptedLLM
from engines.common.provenance import validate_card


def test_sources_and_invented_facts():
    card = CardDraft(fields={"data": CardField(value="У нас есть 12 файлов для Acme",
                                                sources=[Source(source_id="draft", quote="Есть 12 файлов")])})
    errors = validate_card(card, {"draft": "Есть 12 файлов. Компания другая."})
    assert any(error["code"] == "ungrounded_fact" for error in errors)
    card.fields["data"].value = "Есть 12 файлов"
    assert validate_card(card, {"draft": "Есть 12 файлов."}) == []


def test_bad_quote_and_no_source():
    card = CardDraft(fields={"need": CardField(value="Нужен бот", sources=[Source(source_id="draft", quote="Нужен сайт")])})
    assert any(e["code"] == "bad_quote" for e in validate_card(card, {"draft": "Нужен бот"}))
    card.fields["need"].sources = []
    assert any(e["code"] == "missing_source" for e in validate_card(card, {"draft": "Нужен бот"}))


def test_normalization_and_null():
    card = CardDraft(fields={"context": CardField(value="Это проект", sources=[Source(source_id="draft", quote="ЁТО  ПРОЕКТ")]),
                             "contact": CardField(value=None)})
    assert validate_card(card, {"draft": "Ёто   проект"}) == []


@pytest.mark.asyncio
async def test_json_repair_once():
    class Schema(CardDraft):
        pass
    model = ScriptedLLM(['{"fields": {}}'])
    result = await parse_or_repair(model, "```json\nbad\n```", Schema)
    assert result.fields == {}
    assert len(model.calls) == 1
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


@pytest.mark.asyncio
async def test_answer_channel_parallel_timeout_and_cancel():
    channel = AnswerChannel(timeout_s=0.03)
    one = asyncio.create_task(channel.ask("one", []))
    two = asyncio.create_task(channel.ask("two", []))
    await asyncio.sleep(0)
    channel.submit("one", [Answer(question_id="Q1", text="ответ")])
    assert (await one)[0].text == "ответ"
    with pytest.raises(asyncio.TimeoutError):
        await two
    three = asyncio.create_task(channel.ask("three", []))
    await asyncio.sleep(0)
    channel.cancel("three")
    with pytest.raises(asyncio.CancelledError):
        await three


@pytest.mark.asyncio
async def test_scripted_empty_response_is_error():
    with pytest.raises(ProviderError):
        await ScriptedLLM([LLMResult(" ")]).generate([])
