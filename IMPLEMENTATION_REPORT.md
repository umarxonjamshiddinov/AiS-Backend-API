# AiS Gateway 0.3.0 — 30 сентября 2026

Аудит: существующий MVP уже имел auth/hashed keys, aliases, permissions, RPM/TPM,
PostgreSQL reservations, Bedrock tools и SSE. Исходные 29 тестов проходили. Git в каталоге
отсутствует. Главные gaps — capability matrix, calendar/concurrency quotas, cleanup
после failures/disconnects и соответствующие admin controls.

Реализованы typed capability registry с adapter ceilings; Converse/ConverseStream и
OpenAI adapters; проверки JSON mode/schema, vision, reasoning; безопасный error contract;
UTC daily/monthly quotas; leases/heartbeats/reaper; атомарный settlement, включая late usage;
переработанные формы admin и необязательный денежный бюджет.

## Файлы

| Область | Изменённые/добавленные файлы |
| --- | --- |
| Registry и API | app/gateway/capabilities.py, schemas.py, providers.py, routes.py |
| Quotas и lifecycle | app/gateway/quota.py, execution.py, middleware.py |
| Адаптеры | app/providers/base.py, bedrock_chat.py, openai_chat.py, images.py |
| Приложение/данные | app/main.py, app/core/config.py, app/db/models.py |
| Admin | app/admin/routes.py, app/static/admin.html, admin.js, admin.css |
| Migration | migrations/versions/0002_capabilities_quotas.py, migrations/env.py, alembic.ini |
| Скрипты/config | scripts/seed_models.py, verify_gateway.py, discover_bedrock_models.py, pyproject.toml |
| Тесты | tests/conftest.py, test_gateway.py, test_api.py (форматирование), test_adapters.py, test_capabilities.py, test_quotas.py, test_stream_lifecycle.py, test_migrations.py, test_openai_client.py |
| Документация | README.md, AUDIT.md, IMPLEMENTATION_REPORT.md, docs/admin-preview.jpg |

Сохранены scripts/start.ps1, local PostgreSQL setup, legacy internal provider/endpoint,
исходная миграция 0001 и requirements.lock.txt. В .runtime находятся только локальные
verification helpers, отчёты и backup; они исключены из Ruff/Git.

## Развёртывание и проверки

- 107 тестов проходят; unit/integration не делают paid inference.
- Есть реальные PostgreSQL concurrency tests, UTC rollover, lease recovery,
  idempotent settlement, OpenAI SDK parsing, streaming до завершения upstream,
  disconnect handling и migration tests с исходными данными.
- Ruff, pip check, JavaScript syntax и PowerShell parser — OK.
- Создан .runtime/backups/ais-before-0002-20260930T175200Z.dump.
- На ais применена migration 0002; Alembic check не обнаружил schema drift.
- Версия 0.3.0 реально запущена штатным scripts/start.ps1 на 127.0.0.1:8000.
- Исходные 2 ключа, 2 маршрута и 3 записи usage сохранены. Добавлены отдельные
  отозванные проверочные ключи, Kimi route и usage публичного smoke.
- Реальная Nova: text, SSE, streaming tool call, tool result — пройдены. В первом
  прогоне tool-result smoke достиг искусственного лимита 32 output tokens;
  после уточнения краткого ответа и лимита 64 повторный прогон завершился с stop.
- Публичные /health и OpenAPI показывают рабочую 0.3.0. Реальный публичный SSE:
  OK, 4 input / 2 output tokens, stop. Проверочные keys отозваны, reservations сняты.
- Формы модели и квот проверены через браузер в ais_test на синтетических данных.

## Доступные маршруты и ограничения

- ais-fast → Bedrock / amazon.nova-micro-v1:0: включён; text, streaming,
  system/developer, tools/results, tool_choice auto/none, temperature.
- ais-kimi-k2.5 → Bedrock / moonshotai.kimi-k2.5: включён с text/streaming.
  ID и account availability подтверждены AWS SDK; Converse — AWS documentation.
  Kimi paid inference не выполнялся; доступ существующим ключам не расширялся.
- gpt-4o → OpenAI / gpt-4o: сохранён отключённым, credential отсутствует.

Structured output / vision / reasoning поддержаны на уровне типизации, проверок и
соответствующих адаптеров, но не заявлены на непроверенных текущих маршрутах.
Bedrock reasoning control и OpenAI parallel toggle для Converse отклоняются явно.
Image reservation пока занимает полный настроенный context; текстовая оценка тоже
консервативная. Unknown usage учитывается оценкой, доступной для admin reconciliation.
Все quota windows и reset semantics описаны в README.

Следующий этап: реальные проверки расширенных возможностей Kimi, OpenAI credential
и smoke, точные token counters. Cloudflare, DNS/Tunnel и другие проекты не менялись.
