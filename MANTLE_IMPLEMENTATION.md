# AiS API 0.4.0 — результат реализации

Проверено 1 октября 2026. Основной экземпляр запущен на **127.0.0.1:8000** штатным `scripts/start.ps1`; публичный **https://your-api-domain.example/ready** и OpenAPI подтверждают версию **0.4.0**. Активный пользователь **owner** создан. Полный порядок получения первого личного ключа и вызовов — в [README.md](README.md).

## Реализовано

Расширены существующие FastAPI / PostgreSQL / SQLAlchemy / Alembic, API keys, permissions, quotas, request lifecycle и admin UI. Добавлены пользователи и владельцы ключей, CLI, полный экспорт моделей и общий Mantle provider на `openai==3.8.0`. Один ключ выбирает модель через MODEL_ID. API keys по-прежнему имеют формат `sk-myais-…`, в БД сохраняется только SHA-256 и prefix. Credentials upstream хранятся в серверной конфигурации и никогда не отправляются клиентам.

Готовы `/v1/models`, `/v1/chat/completions`, `/health`, `/ready`, admin users/key management/model management и серверный `complete_safety`. 16 native vision моделей получают изображения напрямую; остальные 24 публичных используют Qwen description → selected model. Два Safeguard скрыты и заблокированы в public inference независимо от key grant. Audio/video и незадействованные параметры не рекламируются как работающие.

## Данные и миграция

Применена **0003_mantle_catalog_users.py**, Alembic head **0003**. Новая таблица — `users`. Расширены `api_keys`, `model_routes`, `usage`; существующая `key_models` сохранена. Есть FK key→user, usage→user/key/model, unique model_key и unique MODEL_ID внутри каталога. Старые ключи связаны с `legacy-owner`; прежние лимиты и permissions не расширялись.

| Проверка рабочей БД | Результат |
| --- | --- |
| Модели из экспорта | **42** |
| Public / internal | **40 / 2** |
| Сохранённые legacy aliases | `ais-fast`, `ais-kimi-k2.5` |
| Физические строки model_routes в существующей БД | **44**: 42 каталога + 2 legacy |
| Строки model_routes в новой БД | **42** |
| Исходные ключи сохранены | **12 / 12** |
| Исходные записи usage сохранены | **10 / 10** |
| Активные reservations после проверок | **0** |
| Временные smoke keys | Все отозваны; их пользователи выключены |

DB `model_routes` — runtime authority; версия JSON из полного экспорта — bootstrap-only. Seed идемпотентен и не перезаписывает изменения администратора. Ни одна из 42 моделей не потеряна; точные MODEL_ID, region, api_route, limits и исходные несовпадения capabilities сохранены. Неуказанный в экспорте model_type оставлен null, без догадок.

До миграции создан backup:

`.runtime/backups/ais-before-mantle-20261001T154121Z.dump`

Также есть архив исходного кода `source-before-mantle-*.zip` без `.env`. Откат миграции защищён от потери новых пользователей и внутренней истории. Исходные данные не удалялись.

## Проверки

- **181 passed** в финальном полном pytest, включая исходные тесты проекта.
- Настоящий PostgreSQL: миграция пустой БД и обновление заполненной 0001 → head, ORM/schema comparison, seed, users/keys, permissions, quota concurrency, usage relations.
- Настоящий OpenAI SDK через локальный HTTP MockTransport: точные endpoints, payloads, отсутствие лишних параметров, redirects, retry count, timeout, error normalization, empty responses, moderation flags и отсутствие secrets в errors/logs.
- Полный HTTP путь с одним ключом для Grok 4.3, Kimi 2.5, GLM 5, Grok 4.6; native/fallback images; private Safety; active/revoked/invalid keys и inactive users.
- Ruff: **All checks passed**. `pip check`: **No broken requirements found**.
- Node syntax и PowerShell parser: **OK**.
- Alembic: **No new upgrade operations detected**.
- Проверены текущие журналы gateway/stage и реальный admin overview: upstream/admin secrets, полные AiS keys и key hashes отсутствуют.

## Реальные inference-вызовы

Использован уже существующий серверный `AWS_BEARER_TOKEN_BEDROCK`, разрешённый как fallback для `OPENAI_API_KEY`. Значение секрета не копировалось в исходники или БД.

| Модель / проверка | Endpoint route | Результат | Tokens |
| --- | --- | --- | --- |
| Grok 4.3, первый probe с output cap 64 | `/openai/v1` | Исчерпан маленький лимит до финального ответа; корректный 502 и сохранённый actual usage | 99 |
| Gemma 3 4B, первый probe | `/v1` | Реальный текстовый ответ | 18 |
| Grok 4.3, output cap 512 | `/openai/v1` | **OK**, finish_reason=stop | 460 |
| Gemma 3 4B тем же AiS key | `/v1` | **Успех**, finish_reason=stop | 18 |
| Gemma 3 4B через **https://your-api-domain.example** | `/v1` | **Успех**, finish_reason=stop | 18 |

Итого пять небольших upstream calls, **613 tokens** по предоставленному usage. Повторный основной прогон подтвердил две модели с разными routes одним AiS key. Публичный прогон подтвердил users/key creation, 40 models, настоящий inference, usage, revoke и отказ отозванному ключу. Все три временных ключа отозваны.

Grok 4.6 / us-west-2 и остальные модели проверены на уровне данных, resolver и SDK transport tests; их live availability не утверждается. Автоматические Safety policy prompts/JSON policy engine не угадывались: предоставлен рабочий внутренний caller с нужными моделями, image routing и лимитами 1024/2048, куда подключается конкретная политика.

## Изменённые и добавленные файлы

| Область | Файлы |
| --- | --- |
| Каталог / модели | `app/catalog/__init__.py`, `app/catalog/cloud_bot_v7.json`, `app/services/models.py` |
| Провайдер | `app/providers/mantle.py`, `app/providers/base.py`, `app/gateway/providers.py` |
| Accounts / Safety / CLI | `app/services/accounts.py`, `app/services/internal_inference.py`, `app/manage.py` |
| БД | `app/db/models.py`, `migrations/versions/0003_mantle_catalog_users.py` |
| API / validation / quotas | `app/api/routes.py`, `app/gateway/routes.py`, `app/gateway/schemas.py`, `app/gateway/capabilities.py`, `app/gateway/quota.py`, `app/gateway/execution.py`, `app/main.py` |
| Админка | `app/admin/routes.py`, `app/static/admin.html`, `app/static/admin.js` |
| Config / dependencies | `app/core/config.py`, `.env.example`, `pyproject.toml`, `requirements.lock.txt` |
| Скрипты | `scripts/start.ps1`, `scripts/seed_models.py`, `scripts/smoke_mantle.py`, `scripts/backup_database.py`, `scripts/verify_gateway.py` |
| Тесты | `tests/test_mantle_client.py`, `tests/test_catalog_gateway.py`, `tests/test_admin_remote.py`, `tests/test_migrations.py`, `tests/test_openai_client.py` |
| Документация | `README.md`, `MANTLE_IMPLEMENTATION.md` |

В рабочем `.env` исправлен только не-секретный `OPENAI_BASE_URL` на Mantle hostname; значения существующих серверных secrets сохранены. Установка проекта в `.venv` обновлена до версии 0.4.0. В `.runtime` находятся backup, launcher metadata, deployment helper и [итоговый машинный отчёт](.runtime/mantle-final-verification.json).

## Перед production

Основной inference flow реализован и запущен; критических TODO для user/key/models/chat/usage нет. Для эксплуатации нужно настроить автозапуск/менеджер службы, мониторинг, расписание backup и проверку восстановления, таймауты reverse proxy/клиентов, AWS entitlements/quotas нужных моделей. Если включаются денежные budgets, предварительно задайте настоящие model prices: экспорт не содержит USD ставок.

Для новой установки заполняются `OPENAI_API_KEY` и, при внешней БД, `DATABASE_URL`; `ADMIN_TOKEN` нужен админке. Локальный setup создаёт DB URLs и ADMIN_TOKEN автоматически. В текущем окружении необходимые credentials уже доступны. Полные переменные — `.env.example`.

Проект `<local-project-path>`, DNS, Cloudflare и настройки Tunnel не изменялись. Проверена существующая цепочка Tunnel → 127.0.0.1:8000.

Документация SDK, использованная при реализации: [OpenAI Python API library](https://developers.openai.com/api/reference/python). Данные моделей взяты из полного пользовательского экспорта, а не из внешнего каталога.
