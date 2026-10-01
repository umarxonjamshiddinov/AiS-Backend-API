# Gateway audit — 2026-09-30

The existing FastAPI application was inspected before changes: README, all routes,
database models, Alembic revision 0001, both provider implementations, quota ledger,
admin UI/API, Windows scripts and the complete test suite. This directory has no Git
repository. No other project, Cloudflare configuration or DNS was accessed.

Baseline: 29 tests passed, including PostgreSQL integration tests in isolated schemas
of `ais_test`; Ruff and `pip check` passed. Production schema was at revision 0001.
Configured routes: enabled `ais-fast` / Bedrock / `amazon.nova-micro-v1:0`; disabled
`gpt-4o` / OpenAI / `gpt-4o`. Bedrock server credential is configured; OpenAI is not.
Two existing downstream keys and three completed usage records must be preserved.

Already implemented: hashed downstream keys, permissions, aliases, RPM/TPM with a
PostgreSQL key-row lock, optional-to-retain cost accounting, real ConverseStream text
and tool deltas, tool results, OpenAI-compatible client responses and a working admin UI.

Gaps: capability metadata and enforcement; external API visibility; multimodal and
structured-output validation; configurable reasoning support; daily/monthly quotas;
concurrency; automatic reservation cleanup; distinct authentication errors; reliable
stream cancellation/failure finalization. The original error path retained monetary
reservations indefinitely and required manual release. Public non-stream requests
also depended unnecessarily on upstream streaming support.

Implementation decisions:

- Extend existing tables with one additive Alembic migration. Preserve aliases,
  permissions, keys, prices and usage. Keep the existing `upstream_model` name as the
  registry's real model ID. Use typed JSON capability metadata, validated against
  each adapter's actual implementation.
- Keep PostgreSQL as the sole quota store. Serialize admission and settlement on
  the internal key ID; use expiring leases with heartbeats for active requests.
  Finalization releases concurrency and reserved budget even on failure. Unknown
  upstream usage is conservatively accounted as an estimate, not held forever as
  an active reservation. UTC calendar boundaries define daily/monthly quotas.
- Keep provider logic behind adapters. Use Converse for non-stream Bedrock calls
  and ConverseStream for SSE. OpenAI structured outputs and reasoning parameters
  must be explicitly enabled per model; no prompt-based JSON-schema emulation.
- Unknown models start with conservative capabilities. Kimi requires a confirmed
  model/profile ID and tested capabilities; no guessed ID or auto-enabled route.
- Keep the public API secret-free and alias-only. Provider credentials are never
  returned from admin APIs. Make budgets/prices optional secondary controls.

Final verification and remaining limitations are recorded in README.md after the work.
