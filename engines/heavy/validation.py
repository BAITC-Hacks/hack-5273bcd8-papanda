"""Additional product constraints, independently implemented for this engine."""
import re

from engines.common.provenance import validate_card
from .state import norm


def card_errors(card, state):
    errors = list(validate_card(card, state.sources))
    for name, field in card.fields.items():
        if field.value is None:
            continue
        def reject(code, message):
            errors.append({"field": name, "code": code, "message": message})
        if name == "contact":
            reject("manual_contact", "Контакт заполняется вручную: движок должен вернуть null")
        if norm(field.value) != norm(" ".join(source.quote for source in field.sources)):
            reject("extractive_value", "Значение должно дословно совпадать с цитатами, соединёнными пробелом")
        for citation in field.sources:
            original = state.sources.get(citation.source_id, "")
            sentences = re.split(r"(?<=[.!?])\s+|[\r\n]+", original)
            allowed = {norm(original), *(norm(s) for s in sentences if s.strip())}
            if norm(citation.quote) not in allowed:
                reject("complete_quote", "Цитируйте источник целиком либо полное предложение, сохраняя отрицания")
            answer = state.answers.get(citation.source_id)
            if answer and state.questions[answer["question_id"]].field != name:
                reject("answer_field", "Ответ относится к другому полю; нельзя переносить его смысл")
    return errors
