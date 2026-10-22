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

* Pytest: **53 passed, 0 failed**, включая 4 обязательных сценария.
* Ruff check и format check: успешно.
* Alembic upgrade на обеих БД, полный downgrade/upgrade на тестовой базе и
  переустановка seed: успешно; `alembic check` — no new upgrade operations.
* HTTP smoke: API реально запущен с `--workers 4`, получены PID
  **6680, 10676, 12408, 15548** в последнем успешном прогоне. Параллельно запущены
  два процесса worker, alpha и beta.
  Проверены 20 платежей, fallback+parallel callback, payload+refund, HTTP 503,
  принятие запроса с потерей ответа и send_timeout, no_webhook+watchdog. Баланс: 92.
* Реальный child process в pytest завершён `os._exit(23)` после COMMIT приёма
  провайдером и до фиксации отправки. После истечения lease две сетевые доставки
  соответствуют одной записи provider_jobs; баланс изменён только одним debit.
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
Локальная сборка контейнера и запуск Compose **не подтверждены**. В CI предусмотрен job `compose`
для настоящей сборки, всех PostgreSQL-тестов и HTTP smoke на Ubuntu.

CI в GitHub имеет два job: локальный Linux-стек с PostgreSQL и Docker Compose.
Наличие workflow само по себе не означает, что CI прошёл. Состояние удалённого
запуска, если он доступен, сообщается отдельно в итоговом ответе.

Репозиторий создан приватным и код отправлен в
[EminSans1/durable-generation-service](https://github.com/EminSans1/durable-generation-service).
Существующие репозитории не изменялись. Проверяйте актуальный CI по вкладке Actions:
во время локальной работы удалённые jobs оставались в очереди без назначенного runner.

Финансовые гарантии относятся к транзакциям PostgreSQL. HTTP delivery может
повторяться; провайдер должен хранить ключ идемпотентности. Production deployment
и интеграции с реальными провайдерами не выполнялись.
