# Durable Generation Service

Сервис генерации изображений и видео на Python 3.12, FastAPI, SQLAlchemy async,
PostgreSQL 17 и HTTPX. Финансовые изменения фиксируются exactly-once в PostgreSQL.
Доставка HTTP к провайдерам допускает повторы; провайдеры дедуплицируют их по
постоянному `attempt_id`. Реальные картинки и видео не создаются: URL результата
имитируются провайдерами согласно заданию.

## Быстрый запуск

Нужны Docker Engine/Desktop с Compose v2. Из корня репозитория:

```bash
docker compose up --build
```

Первый запуск создаёт БД, применяет миграции, каталог и пользователя `id=1` с
нулевым балансом. API: [localhost:8000/docs](http://localhost:8000/docs),
alpha: `localhost:8001`, beta: `localhost:8002`.

`migrate` завершает `alembic upgrade head` до старта API, worker и провайдеров.
PostgreSQL, API и оба провайдера имеют healthcheck. Worker стартует после их
готовности. Данные, очередь и provider idempotency сохраняются в volume `pgdata`.
Остановка без удаления данных: `docker compose down`.

`.env` не обязателен для локального запуска; значения по умолчанию согласованы с
`.env.example`. При необходимости скопируйте его в `.env` и настройте параметры.
Это учебные секреты; для другого окружения нужны отдельные случайные значения.
Порты Compose привязаны к loopback. API пользователей по условию принимает
`user_id`: аутентификация конечных пользователей и реальный биллинг вне объёма задания.

**Статус проверки:** в среде разработки Docker отсутствовал. Команда запуска
была вызвана и завершилась ошибкой отсутствующего executable. Сборка образа и
контейнерный запуск здесь не подтверждены. Вместо них проверен локальный стек с
настоящей PostgreSQL 17.11, четырьмя процессами Uvicorn и двумя worker.
Подробные результаты — [docs/VERIFICATION.md](docs/VERIFICATION.md).

## Архитектура

```mermaid
flowchart LR
    Client[Клиент] --> API[FastAPI: 4 Uvicorn workers]
    Payment[Подписанный платёж] --> API
    API --> PG[(PostgreSQL: users, ledger, tasks, attempts, outbox)]
    Worker[Dispatcher + watchdog] <--> PG
    Worker --> Alpha[Alpha: durable fake provider]
    Worker --> Beta[Beta: durable fake provider]
    Alpha -->|HMAC webhook| API
    Beta -->|HMAC webhook| API
    Alpha <--> Jobs[(PostgreSQL: provider_jobs, provider_settings)]
    Beta <--> Jobs
```

```text
app/api.py          HTTP endpoints и чтение согласованных snapshot
app/schemas.py      Pydantic-валидация
app/service.py      финансовые операции и общие переходы состояния
app/models.py       SQLAlchemy-модели, ограничения и индексы
app/worker.py       durable outbox, lease, fencing и watchdog
app/provider.py     alpha/beta, durable callback queue, сценарии
app/security.py     HMAC по raw body
migrations/        самостоятельные версии схемы, триггеры, seed
tests/             интеграционные PostgreSQL-тесты
scripts/           smoke и локальный многопроцессный стенд
docs/              план, сопоставление с заданием и результаты проверки
```

Общая PostgreSQL для fake providers выбрана для простоты воспроизводимого стенда.
Их таблицы отделены от бизнес-таблиц; провайдеры не изменяют баланс и задачи API.
Шаблон async Alembic адаптирован из установленного официального
`alembic/templates/async/env.py`; SQLAlchemy и HTTPX предоставляют пулы, транзакции
и HTTP transport. Redis, Celery и дополнительные микросервисы не требуются.

## Каталог и правила fallback

| Модель | Цена | Нога 1 | Нога 2 |
|---|---:|---|---|
| `image-fast` | 2 | alpha, до 4 refs | beta, до 10 refs |
| `video-long` | 25 | beta, до 2 refs | alpha, до 1 ref, skip_on_moderation |

Неподходящие по числу refs ноги пропускаются до списания. Например, `image-fast`
с 5 refs сразу запускается на beta; 11 refs дают 400 без финансовой записи.

`technical`, HTTP 5xx, ошибка сети, timeout отправки или ожидания результата
переводят на следующую подходящую ногу. `moderation` запоминается в задаче и
исключает все последующие ноги с `skip_on_moderation=true`. `payload` немедленно
останавливает цепочку. HTTP 4xx от провайдера трактуется как payload; некорректный
ответ 202 — как technical. Когда ног больше нет, задача получает `failed` и refund.

## Финансовый журнал и блокировки

`ledger.amount` — знаковая величина: `payment > 0`, `debit < 0`, `refund > 0`.
Начальный баланс всегда 0. Триггер **AFTER INSERT** в ledger обновляет баланс в
той же транзакции; ограничение `CHECK(balance >= 0)` исключает овердрафт.
AFTER принципиален: конфликтующий `ON CONFLICT DO NOTHING` не запускает изменение
баланса. UPDATE/DELETE ledger и прямое изменение users.balance запрещены триггерами.
Сумма и владелец debit/refund дополнительно сверяются с исходной задачей.
Администратор PostgreSQL может отключить триггеры; приложение этого не делает.
TRUNCATE используется только для сброса выделенной тестовой базы.

Таким образом, после каждой успешной транзакции `users.balance = SUM(ledger.amount)`.
Журнал содержит `user_id`, тип, сумму, `task_id` либо `payment_id`, время и ID.
Последние 50 операций доступны через API в согласованном REPEATABLE READ snapshot.

Все бизнес-переходы получают блокировки в порядке **user → task → attempt → outbox**.
Создание задачи блокирует user через `SELECT FOR UPDATE`, затем проверяет активные
задачи и баланс. Эта же блокировка сериализует платёж, refund и завершение задачи
пользователя, поэтому лимит 3 не нарушается конкурентными POST. PostgreSQL работает
в READ COMMITTED; проверка количества выполняется отдельным SQL после получения
пользовательской блокировки и видит уже зафиксированные задачи предыдущего запроса.
Разные пользователи обрабатываются независимо. Медленного HTTP внутри транзакции нет.

### Exactly-once payment, debit, refund

* Глобальное `UNIQUE(payment_id)` и `INSERT ON CONFLICT DO NOTHING RETURNING` дают
  одно начисление даже при 20 одновременных доставках. Повтор с другим user/tokens
  получает 409. Pending/failed не резервирует payment_id; последующий completed
  может быть начислен.
* `UNIQUE(task_id, kind)` разрешает только один debit и один refund на задачу.
* Webhook и watchdog блокируют одинаковые строки и вызывают `finish_locked`.
  Уже завершённая либо неактуальная попытка ничего не меняет. Терминальное
  состояние, финансовая запись и новый outbox фиксируются атомарно.
* При падении внутри webhook до COMMIT PostgreSQL откатывает весь переход вместе
  с изменением баланса. Повтор доставки может безопасно выполнить его заново.
  При падении после COMMIT, до HTTP-ответа, повтор распознает терминальную попытку.
* Повтор `POST /generate` является новым заказом: клиентский ключ идемпотентности
  создания заказа в контракте не задан. Финансовая уникальность относится к task_id.

### Outbox и восстановление

Создание task, debit, attempt и outbox происходит в одной транзакции.
Dispatcher выбирает outbox через `FOR UPDATE SKIP LOCKED`, сохраняет lease и
случайный fencing token, делает COMMIT и только затем вызывает HTTPX. После ответа
он берёт бизнес-блокировки и проверяет token. Старый владелец после перевыдачи lease
не может применить ответ. Lease по умолчанию 15 с, HTTP timeout — 5 с.

Если процесс погиб между COMMIT и отправкой либо после приёма провайдером, lease
истечёт и другой worker повторит HTTP с прежним `Idempotency-Key: attempt_id`.
Fake provider сохраняет `(provider, attempt_id)` как PRIMARY KEY и содержимое
запроса до ответа 202. Повтор возвращает прежний external_id; изменённое содержимое
с тем же ключом получает 409. Это сохраняется после перезапуска провайдера.

Если ответ потерян из-за сетевого timeout, текущая попытка помечается `send_timeout`
и применяется fallback. Провайдер мог продолжить работу; его поздний webhook
игнорируется. Если worker погиб раньше фиксации timeout, восстановленный worker
повторяет тот же ключ. Callback может прийти раньше ответа POST — он имеет право
завершить queued попытку, а последующий ответ отправителя не меняет её состояние.

Это **не обещание абсолютной exactly-once доставки HTTP**. Сеть допускает повторные
запросы и потерянные ответы; одну логическую попытку обеспечивает durable
идемпотентность провайдера. Exactly-once финансовые изменения обеспечены в БД.
Для реального внешнего провайдера требуется договорённость о ключе и сроке хранения
идемпотентности; без неё нельзя исключить повторную внешнюю работу.

### Watchdog и поздние результаты

`PROVIDER_TIMEOUT=60` задаёт deadline с момента **создания попытки**, включая очередь
и отправку. Поэтому задача не зависает навсегда даже при отсутствии dispatcher.
Watchdog выбирает до 100 просроченных попыток за проход и проверяет каждую под теми
же упорядоченными блокировками. Несколько watchdog могут выбрать один ID, но
второй после блокировки увидит завершённое состояние и ничего не сделает.
Время берётся из PostgreSQL `clock_timestamp()`, сравнение повторяется после блокировки.

Просроченный результат также отклоняется самим обработчиком webhook, даже если
watchdog ещё не успел опросить БД. Граница однозначна: `now >= deadline` означает
timeout. Если completed применён до deadline, watchdog видит терминальную попытку.

**После timeout попытка никогда не воскресает.** Поздний completed получает
HTTP 200 с `applied=false`: он не меняет failed, refund и результат следующей ноги.
Правило исключает противоречие между уже возвращёнными токенами и поздним успехом.

API имеет четыре независимых пула и процесса; координация находится в PostgreSQL.
Worker не запускается внутри API lifespan, поэтому число API-процессов не умножает
фоновые циклы. Можно запускать несколько отдельных worker. Smoke проверяет два.

## API и примеры curl

Первое пополнение пользователя 1 (Bash с OpenSSL; тело подписывается без добавления newline):

```bash
BODY='{"payment_id":"payment-123","user_id":1,"tokens":100,"status":"completed"}'
SIGNATURE=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "${PAYMENT_SECRET:-local-payment-secret}" -hex | awk '{print $NF}')
curl -sS http://localhost:8000/webhooks/payments \
  -H 'Content-Type: application/json' -H "X-Signature: $SIGNATURE" --data-binary "$BODY"

curl -sS http://localhost:8000/generate -H 'Content-Type: application/json' \
  -d '{"user_id":1,"model_slug":"image-fast","prompt":"A beautiful landscape","ref_images":[]}'
# {"task_id":"..."}

curl -sS http://localhost:8000/tasks/REPLACE_WITH_TASK_UUID
curl -sS http://localhost:8000/users/1/balance
curl -sS http://localhost:8000/health
```

`POST /generate` возвращает 202; ошибки: 404 пользователь/модель не найдены,
400 нет подходящих ног, 402 недостаточно токенов, 429 уже три активные задачи,
422 ошибка формата. Референсы — HTTP(S) URL. При одновременном превышении лимита и
недостаточном балансе приоритет имеет 429. Списание и создание задачи атомарны.

`GET /tasks/{id}`: status, current_provider, current_leg, result_url, даты задачи;
attempts с attempt_id, provider, leg, status, external_id, error_type,
started_at, deadline, finished_at. queued уже считается активной попыткой.
Типы ошибок: technical, moderation, payload, http_5xx, send_timeout, timeout.

`POST /webhooks/providers/{alpha|beta}` принимает, например:

```json
{"attempt_id":"UUID","external_id":"UUID","status":"completed","result_url":"https://results.example/result"}
```

Для ошибки: `status=failed`, `error_type=technical|moderation|payload` без result_url.
Заголовок `X-Signature` — hex HMAC-SHA256 **сырых байтов** тела, отдельный секрет
на каждого провайдера. Неверная/отсутствующая подпись даёт 401. Ответ `applied`
показывает, выполнен ли переход. Provider name и external_id сверяются с попыткой.

## Сценарии провайдеров

Без настройки провайдер случайно выбирает completed/technical/moderation/payload
и задержку 2–20 с. Сценарий меняется отдельно для каждого провайдера и хранится в БД:

```bash
curl -sS http://localhost:8001/admin/scenario \
  -H 'Content-Type: application/json' -H 'X-API-Key: local-provider-api-key' \
  -d '{"scenario":"technical","delay":2}'
curl -sS http://localhost:8002/admin/scenario \
  -H 'Content-Type: application/json' -H 'X-API-Key: local-provider-api-key' \
  -d '{"scenario":"success","delay":2}'
```

| scenario | Поведение |
|---|---|
| `random` | Случайный исход, обычная задержка; сброс настройки |
| `success` | completed + URL |
| `technical` | failed technical, следующая нога |
| `moderation` | failed moderation, исключить skip_on_moderation |
| `payload` | failed payload, остановка и refund |
| `http503` | Синхронный HTTP 503 без принятия новой задачи |
| `timeout` | Сначала durable приём, затем ответ задержан на HTTP_TIMEOUT + 2 с |
| `duplicate` | Два одинаковых webhook последовательно |
| `parallel` | Два одинаковых webhook параллельно |
| `no_webhook` | Принятое задание не отправляет callback |
| `late` | completed не раньше PROVIDER_TIMEOUT + 2 с |

`delay` — точное число секунд, допускает 0 для детерминированных интеграционных
тестов; `null` возвращает случайную задержку. Для `late` применяется максимум
переданной задержки и PROVIDER_TIMEOUT + 2. Для `timeout` ставьте `delay` больше
HTTP_TIMEOUT, если нужен timeout раньше callback. Настройка влияет на **новые**
ключи: принятая задача сохраняет исходный сценарий. Несколько ключей с одним
сценарием используют независимые durable jobs.

Callback sender читает provider_jobs с lease/SKIP LOCKED и повторяет доставку при
ошибках HTTP или рестарте. После COMMIT результата API и потери ответа callback
может повториться — его обработчик идемпотентен. Админка и POST /tasks защищены
`X-API-Key`; ключ для исходящих запросов задаётся `PROVIDER_API_KEY`.

## Конфигурация

| Переменная | По умолчанию | Значение |
|---|---|---|
| DATABASE_URL | PostgreSQL URL | SQLAlchemy asyncpg DSN |
| PROVIDER_TIMEOUT | 60 | Deadline попытки, секунды |
| HTTP_TIMEOUT | 5 | HTTP timeout отправки и callback |
| DISPATCH_LEASE | 15 | Lease отправителя; должен превышать HTTP_TIMEOUT |
| WORKER_POLL | 0.25 | Интервал idle poll/watchdog |
| ALPHA_URL / BETA_URL | http://alpha:8001 / http://beta:8001 | Адреса провайдеров |
| CALLBACK_BASE_URL | http://api:8000 | Адрес API из сети провайдера |
| ALPHA_SECRET / BETA_SECRET | локальные значения | HMAC провайдеров |
| PAYMENT_SECRET | локальное значение | HMAC платежей |
| PROVIDER_API_KEY | локальное значение | Доступ к fake providers |
| PROVIDER_NAME | alpha | Выбор приложения провайдера |
| PROVIDER_DELAY_MIN / MAX | 2 / 20 | Обычная задержка callback |
| LOG_LEVEL | INFO | Уровень логирования |

Логи содержат PID, task_id, attempt_id, тип ошибки и состояние. Тела запросов,
prompt и секреты приложением не логируются. Исключения worker логируются;
незафиксированные операции и leases остаются доступными после восстановления БД.

## Миграции, пользователи и тесты

```bash
docker compose run --rm migrate
docker compose --profile test run --rm tests
```

Тестовый контейнер устанавливает dev-зависимости из uv.lock, мигрирует отдельную
`generation_test` и запускает Ruff и pytest. База создаётся init-скриптом PostgreSQL
на новом volume. Для уже существующего volume без этой базы:

```bash
docker compose exec db sh -c 'createdb -U "$POSTGRES_USER" generation_test'
```

Локально нужен Python 3.11+ (проект использует 3.12), [uv](https://docs.astral.sh/uv/)
и настоящая PostgreSQL. SQLite не поддерживается. Создайте две отдельные базы:

```bash
uv sync --frozen
export DATABASE_URL='postgresql+asyncpg://generation:generation@127.0.0.1:5432/generation'
uv run alembic upgrade head
uv run alembic check
# Создать новую миграцию после изменения app/models.py:
uv run alembic revision --autogenerate -m 'Describe schema change'
# Просмотрите сгенерированный SQL, затем примените upgrade head.
# Триггеры Alembic autogenerate не сравнивает; изменения в них пишутся явно.

export TEST_DATABASE_URL='postgresql+asyncpg://generation:generation@127.0.0.1:5432/generation_test'
DATABASE_URL="$TEST_DATABASE_URL" uv run alembic upgrade head
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
DATABASE_URL="$TEST_DATABASE_URL" uv run python -m scripts.run_local_stack
```

В PowerShell используйте `$env:DATABASE_URL='...'` и `$env:TEST_DATABASE_URL='...'`.
Тесты и `run_local_stack` **очищают** только таблицы выделенной базы, имя которой
обязательно начинается с `generation_test`. Не запускайте их одновременно на одной
базе. Основную БД они не очищают. Standalone smoke для запущенного Compose:

```bash
uv run python -m scripts.smoke
```

Smoke использует пользователя 1, пополняет его на 100 и создаёт 5 задач. На время
проверки он меняет сценарии и в finally возвращает `random`. Запускайте на
неиспользуемом демонстрационном стенде. Локальный runner сам запускает и останавливает
4 API-процесса, alpha/beta и 2 worker; порты 18000–18002, логи в `.tools/smoke/`.

Добавление другого пользователя без отдельного административного API:

```bash
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "INSERT INTO users(id) VALUES (2)"'
```

Тесты проверяют строки БД, финансовый журнал и инварианты, включая 20 платежей,
10 возвратов, конкурентные debit/лимит, все правила fallback, late completed,
no webhook, HMAC, rollback, lease/fencing, provider idempotency после рестарта и
аварийное завершение отдельного процесса после приёма провайдером. Для timeout
БД-дедлайны сдвигаются SQL, ожидания случайной задержки отсутствуют. Сетевой smoke
использует настоящие HTTP-запросы и короткий timeout; рабочее значение 60 с допустимо.

## Ограничения и эксплуатационные границы

* Ссылки результатов фиктивные. Пользовательская аутентификация, rate limit на
  входящие webhook, хранение бинарных файлов и production observability не входят в задание.
* Подписанные callback не имеют отдельного timestamp: повтор безопасен благодаря
  состоянию попытки и финансовым UNIQUE. В реальной интеграции можно согласовать
  окно подписи и ротацию секретов.
* Каталог считается неизменным во время выполнения задач; публичного API изменения
  каталога нет. Цена сохраняется в task для точного возврата первоначальной суммы.
* Доставка после сбоя продолжается при восстановлении PostgreSQL и хотя бы одного
  worker. Fake providers хранят ключи без очистки; очистка требует согласованного
  срока идемпотентности. Dispatcher отправляет последовательно в каждом worker;
  для большей пропускной способности можно добавить worker, сохраняя блокировки.
* Не заявляется протестированный контейнерный запуск в текущей Windows-среде.
  Репозиторий содержит Compose и CI для воспроизводимой проверки на Docker/Linux.

Механизмы опираются на документированные свойства PostgreSQL:
[изоляция транзакций](https://www.postgresql.org/docs/17/transaction-iso.html) и
[SELECT FOR UPDATE / SKIP LOCKED](https://www.postgresql.org/docs/17/sql-select.html).
