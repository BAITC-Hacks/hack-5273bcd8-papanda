"""Generate explicitly synthetic, source-backed demo fixtures (no LLM calls)."""
import json
from pathlib import Path
from app.contracts import Card, CardField, Source, Task, Team, Proposal


EXAMPLES = [
    ("Образование", "Помощник учебного центра", "Хотим чат-бота на ИИ для клиентов, данных пока нет, нужно к следующему месяцу.", {
        "context": "Администраторы учебного центра вручную отвечают на повторяющиеся вопросы о курсах.",
        "need": "Сократить повторяющиеся ответы администратора и дать клиентам доступ к расписанию.",
        "users": "Будущие слушатели курсов и администраторы учебного центра.",
        "data": "Данных пока нет; CSV с расписанием и 30 обезличенных вопросов подготовит бизнес.",
        "constraints": "Срок прототипа — 4 недели; использовать Python и только публичные учебные данные.",
        "expected_result": "Веб-прототип помощника по расписанию с передачей неизвестного вопроса администратору.",
        "success_criteria": "Не менее 80% из 30 тестовых вопросов получают ответ со ссылкой на расписание.",
        "contact": "courses@example.test", "interaction_format": "Консультация с представителем бизнеса раз в неделю, обратная связь в течение двух дней.",
    }),
    ("Торговля", "Прогноз остатков магазина", "Магазин хочет уменьшить списания. Есть CSV продаж за год. Нужен отчёт за три недели.", {
        "context": "В небольшом магазине часть скоропортящихся товаров списывается из-за избыточных заказов.",
        "need": "Помочь закупщику оценивать потребность в товарах на следующую неделю.",
        "users": "Закупщик магазина и управляющий торговой точкой.",
        "data": "Есть обезличенный CSV продаж за 12 месяцев: дата, товар, количество и остаток.",
        "constraints": "Срок 3 недели; Python; данные доступны только в предоставленном учебном CSV.",
        "expected_result": "Отчёт с недельным прогнозом и сравнением с простым средним по предыдущим неделям.",
        "success_criteria": "Ошибка прогноза не более 20% на отложенных последних четырёх неделях.",
        "contact": "shop@example.test", "interaction_format": "Закупщик проверяет промежуточный отчёт каждую пятницу и отвечает в течение суток.",
    }),
    ("Экология", "Карта точек переработки", "Хотим карту пунктов переработки для жителей. Есть открытый список адресов.", {
        "context": "Жителям района трудно найти действующие пункты приёма разных видов вторсырья.",
        "need": "Собрать доступные пункты приёма на одной карте с фильтрами по типу материала.",
        "users": "Жители района, которые самостоятельно сортируют бытовые отходы.",
        "data": "Открытая таблица CSV с адресами пунктов, координатами и типами принимаемого сырья.",
        "constraints": "Срок 2 недели; использовать только открытые данные и бесплатную картографическую библиотеку.",
        "expected_result": "Интерактивная карта с фильтром по материалу и ссылкой на исходный список адресов.",
        "success_criteria": "Все 25 пунктов из таблицы видны на карте, фильтры проходят 5 подготовленных сценариев.",
        "contact": "recycle@example.test", "interaction_format": "Куратор проверяет адреса перед демонстрацией и отвечает на вопросы дважды в неделю.",
    }),
    ("Логистика", "Планировщик доставки", "Доставка идёт долго. Хотим понятный план маршрутов, есть XLSX заказов и ограничения водителя.", {
        "context": "Диспетчер небольшой службы доставки вручную распределяет адреса между двумя маршрутами.",
        "need": "Помочь диспетчеру составлять маршруты с учётом времени доставки и вместимости.",
        "users": "Диспетчер службы доставки и руководитель смены.",
        "data": "Синтетический XLSX с 40 заказами: координаты, объём и допустимый интервал доставки.",
        "constraints": "Срок 4 недели; не более двух машин, каждая вместимостью 20 условных единиц.",
        "expected_result": "Редактируемый план двух маршрутов с видимыми нарушениями заданных ограничений.",
        "success_criteria": "На тестовом наборе 40 заказов нет превышения вместимости и все интервалы проверены.",
        "contact": "routes@example.test", "interaction_format": "Диспетчер показывает текущий процесс на встрече и проверяет результат раз в неделю.",
    }),
    ("Культура", "Навигатор музейных событий", "Музею нужна страница событий. Есть календарь и описания, важна доступность информации.", {
        "context": "События музея публикуются в разных каналах, посетителям трудно найти актуальное расписание.",
        "need": "Объединить события в доступном каталоге с фильтрами по дате и формату посещения.",
        "users": "Посетители музея и сотрудники информационной стойки.",
        "data": "JSON с 20 вымышленными событиями, датами, описаниями и ссылками на материалы.",
        "constraints": "Срок 2 недели; браузерное приложение без регистрации и без сбора персональных данных.",
        "expected_result": "Страница каталога событий с понятным расписанием и поиском по названию.",
        "success_criteria": "Все 20 событий доступны; поиск и фильтры проходят не менее 6 тестовых сценариев.",
        "contact": "museum@example.test", "interaction_format": "Сотрудник музея проверяет макет и отвечает на вопросы по электронной почте раз в два дня.",
    }),
]


def build():
    tasks, teams, proposals = [], [], []
    # All fields exist; increasingly complete confirmations demonstrate distinct readiness levels.
    confirmed_sets = [
        {"title", "context", "need"},
        {"title", "context", "need", "users", "data"},
        {"title", "context", "need", "users", "data", "constraints", "expected_result"},
        {"title", "context", "need", "users", "data", "constraints", "expected_result", "success_criteria", "contact", "interaction_format"},
        {"title", "context", "need", "users", "data", "constraints", "expected_result", "success_criteria", "contact", "interaction_format"},
    ]
    for i, (industry, title, draft, values) in enumerate(EXAMPLES, 1):
        stamp = f"2026-09-23T08:{i:02d}:00+00:00"
        tasks.append(Task(id=f"draft-{i}", text=draft, industry=industry,
                          created_at=stamp, sources={"draft": draft}).model_dump(mode="json"))
        full = {"title": title, **values}
        # Published low-readiness examples use genuinely absent fields, never unconfirmed ones.
        fields = {name: CardField(value=value if name in confirmed_sets[i-1] else "",
                                  status="confirmed" if name in confirmed_sets[i-1] else "empty",
                                  sources=[Source(source_id=f"seed-{name}", quote=value)]
                                  if name in confirmed_sets[i-1] else []) for name, value in full.items()}
        task = Task(id=f"task-{i}", text=draft, industry=industry, status="published",
                    card=Card(fields=fields), created_at=stamp, published_at=stamp,
                    sources={"draft": draft, **{f"seed-{k}": v for k, v in full.items()}})
        # Scoring imported only after domain module integration. Store also recalculates at seed time.
        try:
            from app.scoring import score
            task.score = score(task.card)
        except ImportError:
            pass
        tasks.append(task.model_dump(mode="json"))
        team = Team(id=f"team-{i}", name=["Qadam", "Data Qanat", "Jasyl Lab", "Route Makers", "Madeniet Tech"][i-1],
                    interests=[industry], skills=["Анализ данных", "Веб-разработка"],
                    technologies=["Python", "React", "SQLite"])
        teams.append(team.model_dump(mode="json"))
        proposals.append(Proposal(id=f"proposal-{i}", task_id=f"task-{i}", team_id=team.id,
                         idea=f"Создадим проверяемый прототип: {title.lower()}.",
                         plan="Уточнить доступные материалы, собрать прототип, проверить критерии и показать результат.",
                         deadline="3 недели после согласования", link=f"https://example.test/prototypes/{i}",
                         created_at=stamp).model_dump(mode="json"))
    return {"tasks": tasks, "teams": teams, "proposals": proposals}


if __name__ == "__main__":
    target = Path(__file__).resolve().parent.parent / "seed/data.json"
    target.write_text(json.dumps(build(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Synthetic seed: 5 drafts, 5 published cards, 5 teams, 5 proposals -> {target.name}")
