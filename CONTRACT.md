# Контракт интеграции AI Sana

Актуальный пользовательский AI-путь — один движок light. TASK.md определяет требования. Контракты продукта — app/contracts.py, движка — engines/common/contracts.py. Схемы доступны на `/api/ai/contract`.

## HTTP продукта

Префикс всех маршрутов — `/api`. Ответы — объекты/массивы без дополнительной обёртки. Отсутствующий объект: 404; недопустимый переход: 409; ошибка схемы: 422.

| Метод и путь | Вход / результат |
|---|---|
| POST /tasks | text, industry → Task, 201 |
| GET /tasks | Task[] рабочего пространства |
| GET /tasks/{id} | Task |
| POST /tasks/{id}/analyze | Необязательный mode: stub, light или engine → RunState, 202; engine означает light |
| GET /tasks/{id}/run | RunState |
| POST /tasks/{id}/answers | Массив answer_id, answer → RunState, 202 |
| PUT /tasks/{id}/card | Поля value и confirmed → Task |
| POST /tasks/{id}/publish | Подтверждённая непустая карточка → Task |
| GET /catalog | Необязательные topic, level → Task[] |
| GET /teams | Team[] |
| GET /leaderboard | Team[] по баллам |
| POST /tasks/{id}/proposals | team_id, idea, plan, deadline, link → Proposal, 201 |
| GET /tasks/{id}/proposals | Proposal[] |
| POST /proposals/{id}/decision | decision: accept или reject → Proposal |
| POST /proposals/{id}/stage | confirmed: true → Proposal; +10 однократно выбранной команде |
| GET /ai/contract | engine, input_schema, output_schema, prompts, schemas, invalid_response_examples |
| GET /health | status, ai_mode, version |

Health показывает серверный режим по умолчанию, а не успешность подключения модели. Frontend выбирает начальный режим из health: stub остаётся stub, light/engine соответствует light. После ошибки AI пользователь может явно выбрать резервный stub через «Продолжить без ИИ».

## Продуктовый AI-сервис

TaskRunService запускает анализ асинхронно, отдаёт состояние и принимает ответы. Один активный прогон на задачу. Во время анализа, ожидания ответов и сборки карточки редактирование и публикация запрещены. Общий срок ограничен.

Адаптер передаёт исходный текст, отрасль, веса и prior_sources — допустимые сохранённые источники предыдущих ответов и правок. Контактные сведения фильтруются. Передача контекста не доказывает безошибочную смысловую обработку всех ограничений.

Состояния продукта: idle, analyzing, waiting_answers, building_card, card_ready, error. Вопрос: answer_id, field, question, why, points_at_stake, node_id. node_id ведёт к проблеме, объясняющей вопрос. Failed/cancelled движка становятся error продукта.

Успешная карточка сохраняется как AI-предложение. Источники проверяются повторно; подтверждение и публикация ручные. Правки становятся пользовательскими источниками; контакт модель не создаёт.

## Контракт простого движка

StartRequest: task_id, draft, industry, field_weights, prior_sources, language=ru. RunView: run_id, task_id, engine=light, status, pending_questions, card, graph, error, stop_reason, metrics.

Состояния: analyzing, waiting_answers, building_card, card_ready, failed, cancelled. Вопрос: question_id, text, field, contradiction_id, why, points_at_stake. Ответ: question_id, text. Один ответ на каждый текущий вопрос; пустой текст допустим на уровне движка.

CardDraft содержит поля value и sources; источник — source_id и quote. Неизвестное поле может быть пустым. Граф различает стороны и элементы, gap, contradiction, question, answer, field. Связи показывают развитие, конфликт, вопросы, ответы и обоснование полей.

Первый круг содержит 3–5 вопросов; всего до двух кругов. Ответы оцениваются относительно найденных проблем. Дополнительный круг возможен при новом неизвестном в значимом поле. card_ready означает необходимость ручного подтверждения, а не публикацию.

## Самостоятельный HTTP-сервис

Необязателен для продукта, который подключает движок внутри процесса.

| Метод и путь | Назначение |
|---|---|
| POST /engine/runs | StartRequest → run_id |
| GET /engine/runs/{id} | RunView |
| POST /engine/runs/{id}/answers | Отправить ответы |
| DELETE /engine/runs/{id} | Отменить |
| GET /engine/runs/{id}/trace | NDJSON-события |
| GET /engine/contract | Промпты, схемы, примеры отказов |
| GET /engine/health | Настроенность ролей, не доказательство успешного вызова |

## Источники и переходы

Первичная карточка извлекается из полных цитат/предложений. Перефразирование допускается после программных ограничений и отдельной модели-судьи; при отказе остаётся цитата. Источник не доказывает объективную истину и безошибочность интерпретации.

Судья плана может быть отключён или недоступен без обязательного останова анализа. Судья перефразирования нужен для принятия новой формулировки. Это разные режимы, не обязательное независимое одобрение каждого этапа.

Низкий балл не блокирует публикацию подтверждённой карточки и отклик. Выбор/отклонение ручные. Этап начисляется однократно; после подтверждённого этапа отклонить команду нельзя. Stub — явный режим, не скрытая подмена AI. В UI есть кнопка «Продолжить без ИИ» после ошибки AI; отдельный переключатель режима скрыт. Переключение выполняется по действию пользователя.
