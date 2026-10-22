# Фактические проверки

Дата: 22 октября 2026, Asia/Baku. Полный аудит требований: [ACCEPTANCE.md](ACCEPTANCE.md).

## Локальное окружение

Windows, Python 3.12.14 (uv), PostgreSQL 17.11 из официального EDB binary archive.
PostgreSQL работает на 127.0.0.1:55432; использованы независимые `generation` и
`generation_test`. Бинарники БД и данные расположены вне Git. Изначальный запуск
initdb в пути с кириллицей завершился ошибкой UTF8; установка в ASCII-путь исправила
причину. Ни SQLite, ни подмена блокировок тестовыми объектами не использовались.

## Выполненные команды

```text
uv python install 3.12
uv sync
initdb -D <local-cache>/data -U postgres -A trust --encoding=UTF8 --locale=C
pg_ctl -D <local-cache>/data -l <local-cache>/postgres.log -o "-p 55432 -h 127.0.0.1" start
createdb -h 127.0.0.1 -p 55432 -U postgres generation
createdb -h 127.0.0.1 -p 55432 -U postgres generation_test
uv run alembic upgrade head
uv run alembic check
uv run alembic downgrade base            # Только выделенная тестовая БД
uv run alembic upgrade head
uv run alembic downgrade bf3f4d658e64    # Проверка повторного применения seed
uv run alembic upgrade head
uv run alembic check
uv run pytest -q --junitxml=.tools/pytest.xml
uv run ruff check .
uv run ruff format --check .
git diff --check
uv run python -m scripts.run_local_stack
docker compose up --build
.tools/docker-compose.exe config --quiet
.tools/docker-compose.exe up --build --wait --wait-timeout 30
```

Для миграций указан DATABASE_URL; для pytest — TEST_DATABASE_URL на `generation_test`.
Trust authentication применена только к изолированному loopback PostgreSQL-стенду,
не к Compose. Финансовые данные этого стенда демонстрационные.

## Результаты

* Pytest: **54 passed, 0 failed**, включая 4 обязательных сценария и независимый
  модульный тест HMAC-SHA256 по опубликованному вектору RFC 4231.
* Ruff check и format check: успешно.
* Alembic upgrade на обеих БД, полный downgrade/upgrade на тестовой базе и
  переустановка seed: успешно; `alembic check` — no new upgrade operations.
* HTTP smoke: API реально запущен с `--workers 4`, получены PID
  **6680, 10676, 12408, 15548** в последнем успешном прогоне. Параллельно запущены
  два процесса worker, alpha и beta.
  Проверены 20 платежей, fallback+parallel callback, payload+refund, HTTP 503,
  принятие запроса с потерей ответа и send_timeout, no_webhook+watchdog. Баланс: 92.
* Реальный child process в pytest завершён `os._exit(23)` после COMMIT приёма
  провайдером и до фиксации отправки. В этом тесте provider route вызывается через
  ASGI transport с настоящей PostgreSQL; после истечения lease две обработки
  HTTP-запроса соответствуют одной записи provider_jobs. Баланс изменён одним debit.
  Физические сетевые соединения отдельно проверены в многопроцессном smoke.
* Первая smoke-проверка обнаружила недостаточную изоляцию стенда: активная задача
  от pytest изменила баланс во время HTTP-проверки. Runner исправлен: перед запуском
  процессов он явно очищает выделенную test-базу. Повторная проверка прошла.
* При повторном холодном старте стандартный 5-секундный timeout клиента smoke
  оказался недостаточным для 20 платежей. Для тестового клиента задано 30 с;
  отдельные fault-сценарии используют HTTP_TIMEOUT=2, PROVIDER_TIMEOUT=8.
  Повторный полный HTTP smoke завершился успешно. Норматив производительности
  входящих запросов заданием не установлен; нагрузочный benchmark не заявляется.

## Границы подтверждения

Локальная команда `docker compose up --build` сначала завершилась ошибкой PowerShell:
`The term 'docker' is not recognized ...`. Затем скачан официальный standalone
Docker Compose v5.6.1 (SHA256 сверена с release asset digest). Его реальная команда
`config --quiet` сначала обнаружила пустое имя проекта из-за кириллического пути;
в compose.yaml добавлено явное `name: durable-generation`, повторная валидация прошла.
Команда standalone `up --build --wait` подтвердила отсутствие Docker Engine:
`failed to connect ... npipe:////./pipe/docker_engine ... The system cannot find the file specified`.
Локальная сборка контейнера и запуск Compose на этой Windows-машине не проверялись.
Контейнерный запуск **подтверждён на Ubuntu в GitHub Actions**, как описано ниже.

## Подтверждённые результаты GitHub Actions

При финальном аудите получены статусы job и их фактические логи для
[запуска 39796242372](https://github.com/EminSans1/durable-generation-service/actions/runs/39796242372).
Оба job проверяли коммит `4007238121088c857cc479881693f619a25aa191`, что подтверждается
checkout SHA в каждом логе:

| Job | Результат | Подтверждённые проверки |
|---|---|---|
| `test` (119884111691) | completed / success | Миграции, Alembic check, Ruff, форматирование, **54 passed**, локальный HTTP smoke |
| `compose` (119884048298) | completed / success | Docker build, Compose up с healthchecks, миграции, **54 passed** внутри контейнера, HTTP smoke, Compose down |

В Docker smoke получены четыре API PID: **8, 9, 10, 11**. Итоговый баланс — 92.
В Linux smoke job `test` получены PID **2881, 2882, 2883, 2884**, баланс — 92.
Тем самым сборка и запуск контейнеров, оба провайдера, подписанные callback,
fallback, send_timeout, watchdog и финансовый результат проверены в реальном стеке.

Последующее обновление README и отчётов фиксирует эти результаты; оно не меняет
проверенный исходный код, тесты, зависимости, миграции или конфигурацию запуска.

Репозиторий создан приватным и код отправлен в
[EminSans1/durable-generation-service](https://github.com/EminSans1/durable-generation-service).
Существующие репозитории не изменялись. Первоначальная очередь GitHub Actions
обработана; результаты подтверждены при финальном аудите, а не только со слов автора.

Финансовые гарантии относятся к транзакциям PostgreSQL. HTTP delivery может
повторяться; провайдер должен хранить ключ идемпотентности. Production deployment
и интеграции с реальными провайдерами не выполнялись.
