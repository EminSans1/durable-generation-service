# Сопоставление с заданием

| Пункт | Реализация | Проверка / ограничение |
|---|---|---|
| 1. Генерация, fallback, refund, recovery | API, service, outbox, watchdog | PostgreSQL tests + многопроцессный HTTP smoke |
| 2. Стек | Python 3.12, FastAPI, PG17, SQLAlchemy, asyncpg, Alembic, Pydantic, HTTPX, pytest, Ruff | uv.lock, миграции и проверки реально выполнены |
| 3. Пользователи, журнал, каталог | Триггеры ledger, CHECK/UNIQUE; seed миграцией | Баланс=SUM, запрет UPDATE/DELETE/direct balance, каталог |
| 4. POST /generate | User lock, validations, atomic task/debit/attempt/outbox | 402/429/400, параллельные списания и лимит |
| 4. Provider webhook | Raw HMAC, ordered locks, current attempt, shared transition | Duplicate/refund/race/late/invalid signature |
| 4. Payment webhook | UNIQUE payment_id, ON CONFLICT, AFTER INSERT trigger | 20 одновременных доставок, одна строка/сумма |
| 4. GET task/balance | REPEATABLE READ snapshots, история/ошибки/даты/связи | HTTP assertions, DB-backed data |
| 5. Alpha/beta | Одно приложение с PROVIDER_NAME, durable jobs | Реальный HTTP для обоих; ASGI сценарии на PG |
| 5. Десять сценариев | success/technical/moderation/payload/http503/timeout/duplicate/parallel/no_webhook/late | Админка с API key; тесты и smoke |
| 6. Fallback | Одна функция finish_locked, moderation_seen, eligibility | Параметризованные проверки + HTTP fallback |
| 7. Финансы exactly-once | AFTER INSERT ledger, UNIQUE, user/task row locks | Конкурентные платежи, debit, refund, DB guards |
| 7. COMMIT → HTTP | Outbox lease, SKIP LOCKED, fencing token | Recovery до/после приёма, реальный os._exit в child process |
| 7. Повтор внешнего запроса | Один attempt UUID; durable provider PRIMARY KEY | 20 concurrent accepts, restart, повтор после crash |
| 8. Watchdog | Clock timestamp, PROVIDER_TIMEOUT, ordered state transition | Параллельные watchdog, отсутствующий callback, HTTP smoke |
| 9. Late completed | Terminal attempts immutable in service, deadline check | После refund и после успеха следующей ноги; race |
| 10. Compose | db/migrate/api(4)/alpha/beta/worker; health/dependencies | Standalone Compose config прошёл; Docker Engine отсутствует: build/run не подтверждены |
| 10. 4 API workers | Uvicorn --workers 4 | Реальный локальный запуск: 4 PID из HTTP /health |
| 11. 4 обязательных теста | Concurrent payments/refunds, fallback, late completed | Все присутствуют и выполнены на PostgreSQL |
| 11. Дополнительные тесты | Validation, HMAC, races, failure/recovery, balance sum | Полный pytest; SQLite отсутствует |
| 12. README | Русский, Mermaid, гарантии/границы, curl, env, миграции/тесты | Самопроверка документации и ссылок |
| 13. Git/качество | Этапные коммиты, ignore secrets, type hints, Ruff, CI | git diff --check, Ruff, история и status |
| 14–15. Весь цикл и отчёт | docs/VERIFICATION.md + итоговый ответ | Честно отделены выполненные проверки и Docker blocker |

Exactly-once относится к финансовым изменениям PostgreSQL. HTTP допускает повторную
доставку; одну логическую попытку у провайдера обеспечивает его персистентный ключ.
Повтор POST /generate — отдельная задача, поскольку ключ заказа контрактом не задан.
