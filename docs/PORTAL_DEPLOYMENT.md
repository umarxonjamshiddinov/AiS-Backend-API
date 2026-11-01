# Public API portal deployment

The prepared frontend bundle in `app/public_portal` contains public HTML, CSS, JavaScript, and font assets. The deployment helper installs this bundle into an existing backend checkout without replacing the backend implementation.

## Deployment requirements

- A Linux host with the backend checkout and its existing virtual environment.
- An existing API service and reverse proxy or tunnel, if used by the deployment.
- A reviewed release commit containing `app/portal.py`, `app/public_portal/`, `scripts/install_portal.py`, and `scripts/deploy_portal.py`.

Use the existing host, checkout, service, and routing configuration for your environment. Do not copy environment-specific hostnames, addresses, credentials, or filesystem paths into public documentation. Preserve unrelated services and existing proxy/tunnel routes.

## Installation

Fetch the reviewed release commit without pulling it into a dirty working tree. Archive only these paths into a new directory under the checkout's `.runtime`:

```text
app/portal.py
app/public_portal/
scripts/install_portal.py
scripts/deploy_portal.py
```

Run the staged `scripts/deploy_portal.py` with the existing backend virtual environment, `--backend-root <backend-checkout>` and `--package-root <extracted-package>`. No Node installation, dependency upgrade, or database migration is required.

The helper validates the bundle, records a backup of `app/main.py` and any active portal manifest, installs an immutable release, appends a small portal hook to the existing application, and verifies the route set plus HTML/HEAD/assets using the application's ASGI interface without starting its lifespan. No inference calls are made. On validation failure, the previous main file and manifest are restored. Previously installed releases remain available.

The module registers only `/`, `/assets`, `/favicon.svg`, `/robots.txt`, and `/sitemap.xml`. It does not add a wildcard route or change `/v1`, `/admin`, `/static`, health, readiness, docs, or other application routes. Portal data is selected by `.runtime/portal/active.json`; an existing `AIS_PORTAL_DIR` environment override takes precedence and must be checked if the portal does not load.

After `PORTAL_READY`, restart the existing API service, then check its local root, `/health`, and unauthenticated `/v1/models`. The root must return HTML and the model list must retain JSON authentication errors. Verify the public portal at `https://<your-api-domain>/` and check any separately hosted services using their configured addresses.

## Rollback

The helper prints the exact `.runtime/portal-backup-*` directory. Restore its `main.py` to `app/main.py` and its `active.json` (when present) to `.runtime/portal/active.json`, then restart the API service. If the backup state says the manifest did not exist, disable the new hook by restoring `main.py`; retained release files do not affect API routing.

Do not run `git reset --hard`, overwrite the remote backend, discard local changes, or run database migrations to install this portal.
