# Centralized Cloud Bot Safety (0.5.0)

The existing 40 public model routes and native/fallback vision adapters remain.
Safety uses fixed, versioned prompts ported from the current Cloud Bot policy,
strict server JSON validation and the existing internal complete_safety() function.
Both Safeguard models remain internal-only; client requests cannot choose a
classifier, change its system policy, request arbitrary inference or receive raw
classifier output. Server settings choose the internal text model and image helper.

An additive migration 0005 adds ApiKey.safety_enabled (false for existing keys).
Only the existing Cloud Bot key was opted in. Other clients retain compatibility.
The flag enables POST /v1/safety/assess AND mandatory chat Safety on the server.
Key auth, revoked/disabled owner checks and selected public-model permissions
apply before any Safety inference. Ordinary keys are denied the mechanism.

The request accepts selected_model (public model permission check), policy_version
(ais_safety_v1/v2), bounded context and an optional base64 image. No classifier
model or policy text is accepted. Response contains only validated fields, usage
and an optional one-use receipt. No provider credentials or raw classifier text.
Adaptive warning/sanction behavior and reviewed policy fingerprint remain in
Cloud Bot, using the same facts/rules; background shadow checks use this endpoint.

Receipts live in bounded process memory for 120 seconds, store hashes rather than
request text, and bind key, selected model, exact current text, history and image.
They are consumed once. Changed/transformed input falls back to server Safety.
Multi-worker/restart loss safely rejects a receipt; current origin uses one worker.
Document/web/catalog transforms may cause a second server check, never a bypass.

Safety admission shares concurrency and has a separate bounded request-rate
counter. It does not debit user token/cost/request totals of normal chat.
Internal Safety usage/cost is retained on server-only rows (no user key ID).
Main chat still reserves once, runs Safety before provider dispatch/native/fallback,
and settles/release on success, block, failure or disconnect. The existing Qwen
fallback usage remains separately in execution_metadata.

No real .env was modified. Production DB backup precedes migration; 0005 is
additive and schema-to-metadata integrity tests pass. Origin updated to 0.5.0.

Verification: API full suite 216 passed; changed-code Ruff and compile passed.
Cloud Bot controlled acceptance without OPENAI_*: 15/15 (text/photo dispatcher,
Safety, native vision, fallback, HTML, history, usage, internal isolation, errors).
Live auth/accounting audit 9/9 including permission denial, 429 without inference,
disabled owner, revoked key, reserve released and internal token telemetry.
All temporary keys were revoked and temporary owners disabled in finally.
Adaptive Safety live cases 2/2. No real Telegram messages sent by automation.
