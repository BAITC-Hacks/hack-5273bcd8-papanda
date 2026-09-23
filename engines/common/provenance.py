"""Conservative, source-based checks for model-generated card fields."""
import re
import unicodedata

from .contracts import CardDraft


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower().replace("ё", "е")
    value = value.translate(str.maketrans({"«": '"', "»": '"', "„": '"', "“": '"', "”": '"'}))
    return " ".join(value.split())


_FACT = re.compile(
    r"(?:https?://[^\s,;]+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|@[\w.-]+|"
    r"\+?\d[\d\s()\-]{5,}\d|\d[\d.,%/-]*|\b[A-ZА-ЯЁ][\w-]+\b)",
    re.UNICODE,
)


def _facts(value: str) -> list[str]:
    found = []
    for match in _FACT.finditer(value):
        fact = match.group().strip(" .,;:!?)")
        if not fact:
            continue
        # A capitalized word at the beginning of a sentence is grammatical,
        # rather than evidence of an entity name.
        if fact[0].isupper() and not any(c.isdigit() for c in fact) and not fact.startswith(("http", "@")):
            prefix = value[:match.start()].rstrip()
            if not prefix or prefix[-1] in ".!?":
                continue
        found.append(fact)
    return found


def validate_card(card: CardDraft, sources: dict[str, str]) -> list[dict]:
    errors: list[dict] = []
    for field, item in card.fields.items():
        value = item.value
        if value is None or not value.strip():
            if item.sources:
                errors.append({"field": field, "code": "empty_has_sources", "message": "Пустое поле не должно иметь источников"})
            continue
        if not item.sources:
            errors.append({"field": field, "code": "missing_source", "message": "У непустого поля нет цитаты-источника"})
            continue
        quoted: list[str] = []
        for source in item.sources:
            text = sources.get(source.source_id)
            if text is None:
                errors.append({"field": field, "code": "unknown_source", "message": f"Неизвестный источник {source.source_id}"})
            elif not source.quote.strip() or normalize(source.quote) not in normalize(text):
                errors.append({"field": field, "code": "bad_quote", "message": f"Цитата отсутствует в источнике {source.source_id}"})
            else:
                quoted.append(source.quote)
        if not quoted:
            continue
        evidence = normalize(" ".join(quoted))
        for fact in _facts(value):
            if normalize(fact) not in evidence:
                errors.append({"field": field, "code": "ungrounded_fact", "message": f"Факт «{fact}» отсутствует в цитатах"})
        if len(value) > 3 * sum(map(len, quoted)):
            errors.append({"field": field, "code": "unsupported_length", "message": "Пересказ слишком длинный относительно цитат"})
    return errors
