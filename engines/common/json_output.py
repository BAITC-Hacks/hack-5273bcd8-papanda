"""Strict JSON extraction with an optional single repair request."""
import json
import re
from pydantic import BaseModel


def extract_json(text: str) -> dict:
    text = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError("Ожидался JSON-объект")
    return result


async def parse_or_repair(client, text: str, schema: type[BaseModel], *, timeout: float = 30):
    try:
        return schema.model_validate(extract_json(text))
    except Exception as first:
        repair = await client.generate(
            [{"role": "system", "content": "Исправь только формат. Верни ровно один JSON-объект, без пояснений."},
             {"role": "user", "content": f"Схема: {schema.model_json_schema()}\nОтвет: {text}\nОшибка: {first}"}],
            json_schema=schema.model_json_schema(), max_tokens=1400, timeout=timeout,
        )
        return schema.model_validate(extract_json(repair.text))
