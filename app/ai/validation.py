"""Deterministic evidence gate; quotations prove provenance, not truth."""
from app.contracts import Card, CardField, FieldName


def normalize(value: str) -> str:
    return " ".join(value.split())


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
        quotes.append(quote)
    if normalize(field.value) != normalize(" ".join(quotes)):
        raise ValueError("Value must equal cited quotations; unsupported interpretation rejected")


def validate_card(card: Card, sources: dict[str, str]) -> None:
    for name, field in card.fields.items():
        if name == FieldName.contact and field.value:
            raise ValueError("AI must not generate manual contact")
        validate_field(field, sources)
