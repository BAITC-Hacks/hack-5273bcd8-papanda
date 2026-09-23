"""Transparent, deterministic readiness rules; only human-confirmed facts count."""
import re
from app.contracts import Card, FieldName, FIELD_LABELS, FIELD_WEIGHTS, Score, Breakdown, Missing


def meaningful(value: str) -> bool:
    return len(value.strip()) >= 20 and len(re.sub(r"\W", "", value)) >= 10


def contact_valid(value: str) -> bool:
    return bool(re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?<!\w)@[A-Za-z0-9_]{3,}", value) or
                re.search(r"\+?\d[\d ()-]{8,}\d", value))


def data_available(value: str) -> bool:
    """Require current materials, conservatively rejecting mixed availability.

    Format names alone do not establish availability. A statement mentioning
    unavailable or planned materials must be clarified before awarding points.
    """
    lower = value.casefold()
    unavailable = (
        r"(?:данных|материалов|источников)\s+(?:пока\s+)?нет"
        r"|нет\s+(?:данных|материалов|доступа|источников)"
        r"|отсутств\w*|недоступ\w*|не\s+доступ\w*|не\s+предостав\w*"
        r"|(?:будут|будет|будем)\s+(?:собран\w*|собират\w*|подготов\w*|предостав\w*|доступ\w*)"
        r"|(?:планиру\w*|предстоит)\s+(?:собра\w*|собират\w*|подготов\w*|получ\w*|предостав\w*)"
        r"|^нет[.!\s]*$"
    )
    if re.search(unavailable, lower):
        return False
    return bool(re.search(r"csv|xlsx?|json|pdf|api|https?://|таблиц|выгруз|опрос|интервью|пример|источник|документ|истори|лог[иов]|запис|файл|баз[ауы]|отч[её]т|каталог|скан|текст|датасет|dataset|crm", lower))


def measurable(value: str) -> bool:
    return bool(re.search(r"\d|%|не менее|не более|ежеднев|еженедел|за (?:час|день|неделю)|к сроку", value.casefold()))


def bounded(value: str) -> bool:
    return bool(re.search(r"срок|недел|месяц|дней|дня|день|час|доступ|технолог|python|java|react|sql|api|бюджет|руб|тенге|только|нельзя|запрещ|не более|без |огранич|до \d|не позднее", value.casefold()))


def field_points(name: FieldName, value: str, confirmed: bool) -> tuple[float, str]:
    maximum = FIELD_WEIGHTS[name.value]
    if not value.strip():
        return 0, "Добавьте сведения и подтвердите поле"
    if not confirmed:
        return 0, "Проверьте и подтвердите сведения"
    if name == FieldName.title:
        return 0, "Название не влияет на рейтинг"
    if name == FieldName.contact:
        return (maximum, "Указан контакт") if contact_valid(value) else (0, "Укажите e-mail, телефон или @handle")
    if not meaningful(value):
        return 0, "Опишите конкретнее: минимум 20 символов"
    if name == FieldName.data and not data_available(value):
        return 0, "Назовите уже доступный источник, формат или пример; недоступные, будущие или неоднозначно доступные материалы не дают баллов"
    if name == FieldName.constraints and not bounded(value):
        return 0, "Укажите срок, технологию, доступ или другую конкретную границу"
    if name == FieldName.success_criteria and not measurable(value):
        return maximum / 2, "Добавьте измеримый порог — сейчас начислена половина баллов"
    return maximum, "Сведения заполнены и подтверждены"


def score(card: Card) -> Score:
    result = Score()
    for name in FieldName:
        field = card.fields.get(name)
        points, reason = field_points(name, field.value if field else "", bool(field and field.status == "confirmed"))
        maximum = FIELD_WEIGHTS[name.value]
        result.breakdown.append(Breakdown(criterion=FIELD_LABELS[name.value], field=name, points=points, max=maximum, reason=reason))
        result.total += points
        if points < maximum:
            result.missing.append(Missing(field=name, max_gain=maximum-points, hint=reason))
    result.level = "priority" if result.total >= 90 else "ready" if result.total >= 70 else "working" if result.total >= 40 else "draft"
    result.missing.sort(key=lambda item: item.max_gain, reverse=True)
    return result
