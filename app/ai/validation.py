"""Deterministic evidence gate; quotations prove provenance, not truth."""
import re
from app.contracts import Card, CardField, FieldName


def normalize(value: str) -> str:
    return " ".join(value.split())


def complete_quotes(value: str) -> set[str]:
    """Conservative sentence boundaries; no clause/word-level fact extraction."""
    normalized = normalize(value)
    return {normalized, *re.split(r"(?<=[.!?])\s+(?=[A-ZА-ЯЁ])", normalized)}


def contains_contact(value: str, known_contact: str = "") -> bool:
    return bool(re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\+\d[\d ()-]{7,}\d", value) or
                (len(known_contact.strip()) >= 6 and known_contact.strip() in value))


def validate_field(field: CardField, sources: dict[str, str]) -> None:
    if not field.value.strip():
        if field.sources:
            raise ValueError("Empty value must not carry citations")
        return
    if not field.sources:
        raise ValueError("Nonempty AI value requires citations")
    quotes = []
    for citation in field.sources:
        quote = normalize(citation.quote)
        if not quote or citation.source_id not in sources:
            raise ValueError("Missing or empty source citation")
        if quote not in normalize(sources[citation.source_id]):
            raise ValueError("Quotation not found in referenced user source")
        if quote not in complete_quotes(sources[citation.source_id]):
            raise ValueError("Quote must preserve a complete source or complete sentence, including negation")
        quotes.append(quote)
    if normalize(field.value) != normalize(" ".join(quotes)):
        raise ValueError("Value must equal cited quotations; unsupported interpretation rejected")


def validate_card(card: Card, sources: dict[str, str]) -> None:
    for name, field in card.fields.items():
        if name == FieldName.contact and field.value:
            raise ValueError("AI must not generate manual contact")
        validate_field(field, sources)


def validate_assignments(card: Card, answer_sources: dict[str, str]) -> None:
    """Do not recycle an answer to one question as evidence for a different field."""
    source_fields = {source_id: name for name, source_id in answer_sources.items()}
    for name, field in card.fields.items():
        for citation in field.sources:
            assigned = source_fields.get(citation.source_id)
            if assigned and assigned != name.value:
                raise ValueError(f"Answer source is assigned to {assigned}, not {name.value}")
        expected = answer_sources.get(name.value)
        if field.value and expected and expected not in {s.source_id for s in field.sources}:
            raise ValueError(f"Field {name.value} must use its explicit current answer source")
