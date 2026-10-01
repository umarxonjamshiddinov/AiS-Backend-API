# Remote admin / Bedrock only — версия 0.3.1

Причина сбоя входа подтверждена: новый HTML получал старый admin.js из кеша
по неизменному /static/admin.js. Public response имел CF-Cache-Status: HIT,
max-age=14400 и обращался к отсутствующим stat-models, stat-reserved, stat-spend.
Авторизация при этом возвращала 200 для правильного ADMIN_TOKEN и 401 для неверного.
Ошибка происходила при отрисовке dashboard после входа.

## Исправления

- HTML/JS/CSS связаны одним snapshot. Content hash включён в URL ресурса;
  HTML не кешируется. Public и local admin загружают идентичный актуальный JS.
- Admin fetch использует relative /admin/api и same-origin. Добавлены обработка
  ошибки входа, состояние кнопки входа и защита от двойного login submit.
- Основной Client base URL всегда https://your-api-domain.example/v1; локальный origin вторичен.
- Runtime и credential configuration используют только Bedrock. OpenAI SDK и
  OpenAI-compatible client API сохранены. Неиспользуемый отключённый gpt-4o удалён
  после проверки отсутствия references/usage. Никакие существующие ключи и usage
  не удалялись; актуальные ais-fast / ais-kimi-k2.5 сохранены.
- Схема БД не менялась: Alembic head 0002, schema check без изменений.
  Перед обновлением создан backup ais-before-remote-admin-20260930T182800Z.dump.

## Изменённые файлы

- app/admin/assets.py (новый), app/admin/routes.py, app/main.py.
- app/static/admin.html, app/static/admin.js.
- app/core/config.py, app/gateway/providers.py, schemas.py, capabilities.py, quota.py.
- app/providers/images.py; app/providers/openai_chat.py удалён.
- scripts/seed_models.py, pyproject.toml, .env.example.
- tests/test_admin_remote.py (новый), test_adapters.py, test_gateway.py.
- README.md, REMOTE_ADMIN_REPORT.md, docs/public-admin.jpg.

## Проверки

- 114 pytest tests passed. Ruff, pip check, Node syntax — OK.
- Alembic current: 0002; check: no new upgrade operations.
- Реальные local /health и /admin — 200. Public /admin и assets — 200.
- Настоящий browser login на https://your-api-domain.example/admin — успешен; неправильный
  ADMIN_TOKEN показывает отказ. Токен и provider credential не менялись.
- Через PUBLIC ADMIN создан временный ключ; через ту же страницу изменены RPM,
  TPM, requests/day/month, tokens/day/month, concurrency, expiration, monetary
  budget, allowed models; проверены disable/enable/revoke. Все поля сверены с
  PostgreSQL и публичным admin API. Ключ отозван через UI.
- Через PUBLIC ADMIN создан и отредактирован временный маршрут: output/context,
  capabilities, enabled и external API. Значения сверены с PostgreSQL. После
  проверки удалён только этот неиспользуемый тестовый маршрут.
- Отдельный временный ключ создан через public admin API: /v1/models вернул
  только ais-fast. Один настоящий публичный Nova SSE запрос вернул OK,
  finish_reason=stop, 6 tokens; usage сохранён, reservation освобождена.
  Этот ключ также отозван, повторная авторизация им возвращает 401.
- Uncaught JavaScript errors / console errors в браузерной проверке: 0.
  Проверены DOM + JS, content hashes и узкий экран. Форма укладывается в viewport.
- Основной URL в реальной публичной панели: https://your-api-domain.example/v1.
- Все созданные в этой проверке ключи отозваны; active reservations = 0.
- Финальная проверка HTML, JS, admin API responses и текущих рабочих логов не
  обнаружила ADMIN_TOKEN, AWS credential или полные downstream keys/hashes.

Cloudflare, DNS, tunnel configuration, основной сайт и другие проекты не менялись.
Штатный scripts/start.ps1 сохранён и использован для запуска версии 0.3.1.
