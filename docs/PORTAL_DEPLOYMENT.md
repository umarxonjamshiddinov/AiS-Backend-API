# Public AiS API website

The prepared frontend bundle in `app/public_portal` is built from `<local-frontend-source>`. Visitors request API access through `support@myais.uz`. The bundle contains only public HTML, CSS, JavaScript and fonts.

This release is additive: the backend's existing `app/main.py` and other API implementation files are not replaced. The deployment helper preserves local server changes, including any newer model capabilities.

## Confirmed production layout

- Ubuntu EC2 host `<deployment-host>`.
- Checkout: `<backend-checkout>`.
- API systemd unit: `ais-api.service`, Uvicorn at `127.0.0.1:8000`.
- Existing Cloudflare Tunnel `ais-api`: `your-api-domain.example` → `http://localhost:8000`, `your-chat-domain.example` → `http://localhost:8080`.

Keep those tunnel routes and the chat service unchanged.

## Installation

Fetch the reviewed release commit without pulling it into the dirty working tree. Archive only these paths into a new directory under the checkout's `.runtime`:

```text
app/portal.py
app/public_portal/
scripts/install_portal.py
scripts/deploy_portal.py
```

Run the staged `scripts/deploy_portal.py` with the existing backend virtual environment, `--backend-root <backend-checkout>` and `--package-root` selecting the extracted package. No Node installation, dependency upgrade or database migration is required.

The helper validates the bundle, records a backup of `app/main.py` and any active portal manifest, installs an immutable release, appends a small portal hook to the existing application, and verifies the route set plus HTML/HEAD/assets using the application's ASGI interface without starting its lifespan. No inference calls are made. On validation failure the previous main file and manifest are restored. Previously installed releases remain available.

The module registers only `/`, `/assets`, `/favicon.svg`, `/robots.txt` and `/sitemap.xml`. It does not add a wildcard route or change `/v1`, `/admin`, `/static`, health, readiness, docs or the chat application. Portal data is selected by `.runtime/portal/active.json`; an existing `AIS_PORTAL_DIR` environment override takes precedence and must be checked if the portal does not load.

After `PORTAL_READY`, restart only `ais-api.service`, then check the local root, `/health` and unauthenticated `/v1/models`. The root must return HTML and the model list must retain JSON authentication errors. Verify the public website at `https://your-api-domain.example/` and existing chat at `https://your-chat-domain.example/`.

## Rollback

The helper prints the exact `.runtime/portal-backup-*` directory. Restore its `main.py` to `app/main.py` and its `active.json` (when present) to `.runtime/portal/active.json`, then restart only the API service. If the backup state says the manifest did not exist, disable the new hook by restoring `main.py`; retained release files do not affect API routing.

Do not run `git reset --hard`, overwrite the remote backend, discard local changes or run database migrations to install this portal.
