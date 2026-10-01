# AiS API 0.5.0

Рабочий OpenAI-compatible backend: FastAPI, PostgreSQL, SQLAlchemy/Alembic и OpenAI SDK 3.8.0. Публичный адрес — **https://your-api-domain.example/v1**, админка — **https://your-api-domain.example/admin**.

Каталог из полного экспорта Cloud Bot v7 содержит **42 модели: 40 public и 2 internal**. Новая БД получает ровно 42 записи. В существующей БД сохранены также два прежних маршрута `ais-fast` и `ais-kimi-k2.5`; их прежние ключи и permissions продолжают работать. Ключ с `all_public_models=true` получает 40 публичных моделей нового каталога; legacy aliases требуют отдельного разрешения.

## Запуск

Все команды ниже — PowerShell из каталога проекта.

```powershell
Set-Location '<local-checkout>'
.\scripts\start.ps1
```

Скрипт запускает локальный PostgreSQL, применяет миграции, выполняет идемпотентный seed и запускает Uvicorn на `127.0.0.1:8000`. Если API уже работает на этом порту, второй экземпляр запускать не нужно. Другой порт: `./scripts/start.ps1 -Port 8001`.

Для существующей внешней PostgreSQL:

```powershell
.\scripts\start.ps1 -SkipLocalDatabaseSetup
```

Отдельные команды без запуска сервера:

```powershell
.\.venv\Scripts\python.exe scripts\setup_local_db.py
.\.venv\Scripts\python.exe -m app.manage init-db
.\.venv\Scripts\python.exe -m app.manage models
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`init-db` сам выполняет `alembic upgrade head` и seed. Ручной SQL не требуется. Для установки в новом checkout нужны Python 3.13+ и PostgreSQL; затем:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Заполните серверный `OPENAI_API_KEY`. Для автоматической локальной БД оставьте `DATABASE_URL` и `TEST_DATABASE_URL` пустыми: `setup_local_db.py` создаст кластер `.runtime/postgres`, базы `ais` / `ais_test`, сохранит пароль только в `.env` и создаст `ADMIN_TOKEN`. PostgreSQL по умолчанию ищется в `C:\Program Files\PostgreSQL\18\bin`; другой путь задаётся `AIS_POSTGRES_BIN`. Внешний `DATABASE_URL` скрипт не перезаписывает.

## Первый пользователь и API key

CLI требует доступ к серверу и его БД. В рабочей БД уже создан пользователь `owner`; команда создания повторно возвращает его без изменения состояния.

```powershell
Set-Location '<local-checkout>'
.\.venv\Scripts\python.exe -m app.manage create-user --name owner
$issued = .\.venv\Scripts\python.exe -m app.manage create-key --user owner --name cloud-bot | ConvertFrom-Json
$aisKey = $issued.key
$aisKey
Set-Clipboard -Value $aisKey
```

Полный ключ `sk-myais-…` выдаётся только при создании. БД хранит SHA-256 случайного 256-bit секрета и короткий prefix. Этот ключ отличается от серверного Bedrock credential. Без `--model` CLI разрешает весь публичный каталог. Для ограниченного ключа повторяйте `--model <MODEL_ID>`.

Управление:

```powershell
.\.venv\Scripts\python.exe -m app.manage list-keys --user owner
.\.venv\Scripts\python.exe -m app.manage revoke-key --id $issued.id
.\.venv\Scripts\python.exe -m app.manage set-user-active --user owner --active false
```

Список не содержит секретов или hashes. Отзыв окончателен. Неактивный пользователь не может использовать ни один свой ключ. `last_used_at` обновляется при успешной аутентификации, включая `/v1/models`.

Основной список админки и счётчик показывают действующие ключи с активным владельцем. Кнопка «Архивированные» открывает отозванные, отключённые и истёкшие ключи с owner, prefix, датами и лимитами. Revoke сохраняет запись, permissions и usage; история остаётся в «Последних запросах». Схема БД не меняется.

RPM и TPM поддерживают настоящий **«Без лимита»**: `NULL` в БД и `null` в admin API. В админке очистите соответствующее поле; при повторном открытии оно останется пустым. Числовые значения продолжают ограничивать ключ. Пропущенное поле при редактировании сохраняет прежнее значение, включая `null`; новые ключи без этих параметров сохраняют defaults 30 RPM / 2 000 000 TPM.

```json
{"name":"cloud-bot","rpm":null,"tpm":null}
```

Это body для `PUT /admin/api/keys/{id}` с ADMIN_TOKEN. При создании через CLI:

```powershell
.\.venv\Scripts\python.exe -m app.manage create-key --user owner --name cloud-bot --rpm unlimited --tpm unlimited
```

Каждый `null` отключает только свою минутную проверку. Дневные/месячные квоты, concurrency, budget и ограничения upstream продолжают действовать. Миграция **0004_nullable_minute_limits.py** меняет только nullable RPM/TPM; существующие лимиты не переписываются. Downgrade требует предварительно назначить числовые RPM/TPM всем безлимитным ключам.

## Один ключ — разные модели

Продолжение в той же PowerShell-сессии после создания ключа, до команды revoke:

```powershell
$base = 'http://127.0.0.1:8000/v1'
# Для публичного доступа: $base = 'https://your-api-domain.example/v1'
$headers = @{ Authorization = "Bearer $aisKey" }

Invoke-RestMethod -Uri "$base/models" -Headers $headers

foreach ($model in @('xai.grok-4.3', 'google.gemma-3-4b-it')) {
    $body = @{
        model = $model
        messages = @(@{ role = 'user'; content = 'Reply with exactly OK.' })
    } | ConvertTo-Json -Depth 6
    Invoke-RestMethod -Method Post -Uri "$base/chat/completions" -Headers $headers `
        -ContentType 'application/json; charset=utf-8' -Body $body
}
```

Тот же ключ принимает, например, `moonshotai.kimi-k2.5`, `zai.glm-5` и остальные разрешённые MODEL_ID. Лимит по умолчанию соответствует экспорту; искусственно маленький `max_tokens` может исчерпаться до появления финального текста.

OpenAI SDK:

```python
import os
from openai import OpenAI

with OpenAI(api_key=os.environ["AIS_API_KEY"], base_url="https://your-api-domain.example/v1",
            max_retries=0, timeout=400) as client:
    print([m.id for m in client.models.list()])
    for model in ("xai.grok-4.3", "moonshotai.kimi-k2.5"):
        reply = client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": "Hello"}]
        )
        print(reply.choices[0].message.content)
```

`max_retries=0` у клиента позволяет избежать умножения серверных retries. Серверный upstream timeout — 120 секунд на попытку; с двумя retries худший случай может занять более 360 секунд.

## Endpoints

| Endpoint | Доступ / результат |
| --- | --- |
| `GET /health` | Liveness |
| `GET /ready` | Проверка БД, migration head и наличия 42 исходных моделей |
| `GET /v1/models` | AiS Bearer key; только public + enabled + разрешённые модели |
| `POST /v1/chat/completions` | AiS Bearer key; OpenAI-style полный ChatCompletion |
| `GET/POST /admin/api/users` | ADMIN_TOKEN; список / создание пользователей |
| `PUT /admin/api/users/{id}` | ADMIN_TOKEN; `{"active": false}` / true |
| `GET /admin/api/users/{id}/keys` | ADMIN_TOKEN; метаданные ключей без секретов |
| `POST /admin/api/keys` | ADMIN_TOKEN; создание ключа с `user_id` |
| `PUT /admin/api/keys/{id}` | ADMIN_TOKEN; permissions, quotas, expiration, active |
| `POST /admin/api/keys/{id}/revoke` | ADMIN_TOKEN; окончательный отзыв |
| `PUT /admin/api/models` | ADMIN_TOKEN; конфигурация моделей в БД |
| `GET /admin/api/overview` | ADMIN_TOKEN; каталог, пользователи, ключи, usage |
| `PUT /admin/api/credentials` | ADMIN_TOKEN; запись нового серверного credential, без чтения секрета |

Пример создания ключа через admin HTTP:

```http
POST /admin/api/keys
Authorization: Bearer <ADMIN_TOKEN>
Content-Type: application/json

{"user_id":"<USER_UUID>","name":"cloud-bot","all_public_models":true,"tpm":2000000}
```

Админка поддерживает те же операции. Старые admin-клиенты без `user_id` используют отдельного владельца `legacy-owner`; прежний формат ключей сохранён. Public API не принимает ADMIN_TOKEN вместо пользовательского ключа.

## Источник истины и модельные маршруты

**PostgreSQL `model_routes` — единственный runtime source of truth.** В inference нет чтения JSON-каталога или 42 ветвей по имени модели. `app/catalog/cloud_bot_v7.json` — неизменяемый bootstrap-снимок полного пользовательского экспорта. Миграция 0003 импортирует его; повторный seed вставляет только отсутствующие записи и сохраняет DB-изменения, enabled-флаги, лимиты, цены и permissions.

```powershell
.\.venv\Scripts\python.exe -m app.manage sync-models
```

После deployment модель редактируется через admin API/UI. `model_key` и MODEL_ID существующей записи каталога постоянны; новый ID добавляется новой записью. Manufacturer хранится отдельно от backend: у всех 42 исходных моделей backend — `amazon_bedrock`. `model_type=null` означает, что точный enum не указан в предоставленном экспорте; он не угадывался.

| Модели | Region | Route / upstream base |
| --- | --- | --- |
| `xai.grok-4.3`, три Gemma 4 | `us-east-1` | `mantle_openai`: `https://bedrock-mantle.us-east-1.api.aws/openai/v1` |
| `xai.grok-4.6` | `us-west-2` | `mantle_openai`: `https://bedrock-mantle.us-west-2.api.aws/openai/v1` |
| Остальные 37 моделей | `us-east-1` | `mantle_default`: `https://bedrock-mantle.us-east-1.api.aws/v1` |

Общий `MantleClient` использует официальный `OpenAI` SDK в ограниченном threadpool существующего async gateway. Wire request соответствует экспорту: `model`, `messages`, `max_tokens`. Никаких boto3, Converse или SigV4 в пути этих 42 моделей. Synchronous SDK выбран для совместимости с существующим lifecycle адаптеров; входящий FastAPI endpoint остаётся async. Legacy Converse adapter обслуживает только ранее существовавшие маршруты.

`OPENAI_BASE_URL` принимается только с HTTPS Mantle hostname. Region и `/v1` либо `/openai/v1` выбираются из DB-записи, независимо от `AWS_REGION`. `your-api-domain.example` — клиентский адрес, его нельзя ставить upstream base. Redirects отключены. SDK retries отключены; собственный retry повторяет connection/timeout, HTTP 429 и 5xx, максимум `AI_RETRY_COUNT` дополнительных попыток.

## Возможности и лимиты

Для Mantle подключены text input/output, system/developer messages и изображения. Зарегистрированные reasoning/video/speech metadata отделены от реально работающих параметров. `stream=true`, tools, temperature, JSON mode/schema, reasoning_effort и audio/video/document payload возвращают `unsupported_capability`. `response_format={"type":"text"}` допустим как отсутствие специального формата. Документы может предварительно читать клиент и присылать извлечённый текст.

- **16 native vision моделей:** исходные text/image_url parts доходят до выбранной модели.
- **24 публичные модели без native vision:** Qwen3 VL получает изображения, возвращает описание с лимитом 2048; выбранная модель получает исходный текст и описание. Helper не меняет выбранную модель и не расходует её пользовательскую inference/token квоту. Его usage и попытки сохранены отдельно в `usage.execution_metadata`.
- Изображения: base64 data URLs PNG/JPEG/GIF/WebP; до 20 изображений, 3.75 MiB decoded на изображение, 8 MiB на HTTP body. Remote URL и explicit image detail не подключены.
- Grok 4.6 сохраняет зарегистрированные efforts low/medium/high/xhigh, но управление ими не подключено. Исходные расхождения Kimi Thinking / Magistral сохранены в metadata.

Обычный output ceiling:

```text
min(AI_DEFAULT_MAX_OUTPUT_TOKENS,
    model.max_output или AI_DEFAULT_MAX_OUTPUT_TOKENS,
    max(256, context_window // 4))
```

При текущем default 4096: Palmyra — **1000**, GLM 4.7 / Flash — **4000**, остальные — **4096**. Запрос принимает один из `max_tokens` / `max_completion_tokens`; превышение ceiling — 400. Реальный max_output из экспорта хранится отдельно от вычисленного ceiling, включая null. Проверка входного контекста консервативная: UTF-8 bytes и framing. Native image reservation использует полный context; fallback учитывает ограниченное текстовое описание. Для всех native vision моделей выберите TPM «Без лимита» или минимум 1 000 000; числовой default CLI остаётся 2 000 000.

## Safety

`openai.gpt-oss-safeguard-20b` и `openai.gpt-oss-safeguard-120b` зарегистрированы internal, скрыты в `/v1/models` и запрещены в public inference даже при явном KeyModel grant. Включение external flag не обходит запрет.

Внутренний серверный механизм — `app.services.internal_inference.complete_safety(messages, model_key=..., adaptive=False)`. Он реально вызывает общий provider, использует 1024 tokens для v1 / 2048 для adaptive, выбирает Qwen для image assessment и пишет отдельную usage-запись без пользовательской квоты. Default — Safeguard 20B, 120B доступна явно. Вызывающий Safety layer передаёт policy prompt и проверяет свой JSON-контракт; сам текст policy/policy engine в экспорте не предоставлен, поэтому новая политика не включалась в публичные запросы автоматически.

## БД, usage и безопасность

Миграция **0003_mantle_catalog_users.py**:

- создаёт `users`;
- связывает существующие `api_keys` с `legacy-owner`, добавляет user FK, last_used_at и all_public_models;
- расширяет `model_routes` runtime-полями и metadata, unique model_key и unique MODEL_ID внутри каталога;
- дополняет `usage` владельцем, FK на модель, model_key, region/route, purpose и execution_metadata;
- импортирует 42 модели без удаления прежних aliases, ключей и history.

`key_models` и существующая quota/lease система сохранены. RPM/TPM скользящие 60 секунд, день/месяц UTC; concurrency и квоты атомарны в PostgreSQL. Успешный ответ освобождает резерв и записывает actual usage. Если upstream не вернул usage, API возвращает `usage: null`, БД помечает расход как estimate. Ошибка/timeout/disconnect также завершают запись и освобождают concurrency; heartbeat/reaper сохраняют восстановление после падений.

Response `id=chatcmpl-<usage.id>` и `X-Request-ID` связывают ответ с записью БД. Записываются user/key/model, время, status, latency, backend, region, route, error category, tokens и helper metadata. Prompts, изображения, ответы, полные пользовательские keys и upstream credentials в ledger не сохраняются. SDK debug payload logging отключён. Секреты не возвращаются из admin overview/list.

Цены из экспорта неизвестны: исходные USD rates равны 0, что отключает денежное начисление в дополнительном ledger, но **не означает бесплатный AWS inference**. При использовании денежных бюджетов администратор должен сначала настроить тарифы.

Ошибки:

| HTTP | Code |
| --- | --- |
| 401 | invalid_api_key, revoked_api_key, disabled_api_key, expired_api_key |
| 403 | inactive_user, model_not_allowed |
| 404 | model_not_found |
| 400 | invalid_request, unsupported_capability, content_filter |
| 429 | upstream_rate_limit либо конкретный gateway quota error |
| 502 | upstream_error |
| 504 | upstream_timeout |

Формат: `{"error":{"message":"…","type":"…","param":null,"code":"…"}}`. Stack traces и SDK error body клиенту не выдаются.

## Конфигурация

| Переменная | Назначение |
| --- | --- |
| DATABASE_URL | PostgreSQL URL; для локального setup заполняется автоматически |
| TEST_DATABASE_URL | Отдельная `ais_test`; тесты создают disposable schemas |
| OPENAI_API_KEY | Серверный Bedrock Mantle bearer, приоритетный credential |
| OPENAI_BASE_URL | Mantle HTTPS base; default east-1 `/v1` |
| ADMIN_TOKEN | Доступ к admin HTTP/UI; CLI его не требует |
| AI_DEFAULT_MAX_OUTPUT_TOKENS | 4096 |
| AI_REQUEST_TIMEOUT_SECONDS | 120 на upstream attempt |
| AI_RETRY_COUNT / AI_RETRY_BACKOFF_SECONDS | 2 / 0.5 |
| VISION_HELPER_MODEL_KEY | qwen3_vl_235b_a22b |
| MODERATION_MODEL_KEY | gpt_oss_safeguard_20b |
| RESERVATION_TTL_SECONDS / HEARTBEAT_SECONDS / REAPER_SECONDS | В `.env.example` указаны полные имена; 180 / 30 / 30 |

Для совместимости существующий `AWS_BEARER_TOKEN_BEDROCK` используется как серверный fallback, когда `OPENAI_API_KEY` пуст. В текущей установке live smoke использовал этот уже настроенный credential. Секрет не копировался в БД или исходники. `AWS_REGION` относится к прежнему Converse adapter; Mantle resolver использует region модели. При нескольких workers ротация credentials требует согласованного перезапуска.

## Проверки и эксплуатация

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m alembic current
.\.venv\Scripts\python.exe -m alembic check
node --check app\static\admin.js

# Только HTTP/auth/catalog, без paid inference:
.\.venv\Scripts\python.exe scripts\smoke_mantle.py
# Два коротких paid запроса разными маршрутами одним временным ключом:
.\.venv\Scripts\python.exe scripts\smoke_mantle.py --live
# Резервная копия основной БД:
.\.venv\Scripts\python.exe scripts\backup_database.py
```

Smoke отзывает временный ключ и выключает временного пользователя. Pytest не делает платных вызовов; integration tests используют реальный PostgreSQL. Подробные результаты, live usage и список изменённых файлов — [MANTLE_IMPLEMENTATION.md](MANTLE_IMPLEMENTATION.md).

Для production остаются настройки эксплуатации: менеджер службы/автозапуск, резервное копирование и проверка восстановления, мониторинг ошибок/таймаутов, реальные денежные тарифы при использовании budget, AWS entitlements/quotas для нужных моделей. Live подтверждены отдельные модели; доступность остальных у AWS нельзя заключить из одного registry. Существующая Cloudflare/Tunnel-схема сохраняется. Проект AiSCloudBot не изменялся.

## Centralized Cloud Bot Safety

See [docs/cloud_bot_safety.md](docs/cloud_bot_safety.md). Safety now executes server-side for opted-in keys; internal models remain inaccessible through public inference.
