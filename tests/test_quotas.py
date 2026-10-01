from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FakeChat, completion, issue
from sqlalchemy import func, select

from app.db.models import ApiKey, Usage
from app.gateway.providers import registry
from app.gateway.quota import authenticate, mark_dispatched, reap_expired, renew, reserve, settle
from app.gateway.schemas import ChatRequest
from app.providers.base import ProviderError

REQUEST = ChatRequest(model="ais-fast", messages=[{"role": "user", "content": "Hi"}], max_tokens=16)
ACTUAL = {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}


@pytest.mark.parametrize("field", ["requests_per_day", "requests_per_month"])
def test_calendar_request_limits(gateway, field):
    client, factory = gateway
    _, _, headers = issue(client, **{field: 1})
    assert completion(client, headers).status_code == 200
    response = completion(client, headers)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limit_exceeded"
    assert response.json()["error"]["param"] == field
    assert int(response.headers["retry-after"]) > 0
    assert len(registry.get("bedrock").calls) == 1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Usage)) == 1


@pytest.mark.parametrize("field", ["tpm", "tokens_per_day", "tokens_per_month"])
def test_token_reservations_replaced_by_actual_usage(gateway, field):
    client, _ = gateway
    _, _, headers = issue(client, **{field: 4000})
    assert completion(client, headers).status_code == 200
    # Two reservations would exceed the limit; actual six-token usage permits this.
    assert completion(client, headers).status_code == 200
    _, _, blocked = issue(client, **{field: 1})
    response = completion(client, blocked)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "token_limit_exceeded"
    assert response.json()["error"]["param"] == field


def test_calendar_reset_preserves_rolling_minute_across_months(gateway, monkeypatch):
    client, _ = gateway
    at = datetime(2026, 1, 31, 23, 59, 30, tzinfo=UTC)
    monkeypatch.setattr("app.gateway.quota.utcnow", lambda: at)
    _, _, headers = issue(client, rpm=1, requests_per_day=1, requests_per_month=1)
    assert completion(client, headers).status_code == 200
    at += timedelta(seconds=31)
    response = completion(client, headers)
    assert response.json()["error"]["param"] == "rpm"
    at += timedelta(seconds=31)
    assert completion(client, headers).status_code == 200


@pytest.mark.parametrize(
    "field", ["max_concurrent_requests", "requests_per_day", "requests_per_month"]
)
def test_parallel_admission_cannot_bypass_limits(gateway, field):
    client, factory = gateway
    key_id, _, _ = issue(client, budget_usd=None, rpm=1000, tpm=1000000, **{field: 2})

    def admit(_):
        try:
            return reserve(key_id, REQUEST)
        except ProviderError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(admit, range(8)))
    admitted = [value for value in results if not isinstance(value, str)]
    assert len(admitted) == 2
    expected = (
        "concurrency_limit_exceeded"
        if field == "max_concurrent_requests"
        else "rate_limit_exceeded"
    )
    assert results.count(expected) == 6
    if field == "max_concurrent_requests":
        settle(admitted[0].id, None, 1, usage_unknown=False)
        assert reserve(key_id, REQUEST).id
        with factory() as db:
            assert (
                db.scalar(select(func.count()).select_from(Usage).where(Usage.status == "reserved"))
                == 2
            )


@pytest.mark.parametrize("field", ["tpm", "tokens_per_day", "tokens_per_month"])
def test_parallel_token_reservations(gateway, field):
    client, factory = gateway
    key_id, _, _ = issue(
        client, budget_usd=None, rpm=1000, tpm=1000000, max_concurrent_requests=None
    )
    probe = reserve(key_id, REQUEST)
    with factory() as db:
        bound = db.get(Usage, probe.id).reserved_tokens
    settle(probe.id, None, 0, usage_unknown=False)
    with factory.begin() as db:
        setattr(db.get(ApiKey, key_id), field, 2 * bound)

    def admit(_):
        try:
            reserve(key_id, REQUEST)
            return "accepted"
        except ProviderError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=6) as pool:
        result = list(pool.map(admit, range(6)))
    assert result.count("accepted") == 2
    assert result.count("token_limit_exceeded") == 4


def test_inflight_tokens_do_not_fall_out_of_minute_window(gateway):
    client, factory = gateway
    key_id, _, _ = issue(client)
    first = reserve(key_id, REQUEST)
    with factory.begin() as db:
        usage = db.get(Usage, first.id)
        usage.created_at -= timedelta(seconds=61)
        db.get(ApiKey, key_id).tpm = usage.reserved_tokens
    with pytest.raises(ProviderError) as caught:
        reserve(key_id, REQUEST)
    assert caught.value.code == "token_limit_exceeded"


def test_key_rechecked_under_lock(gateway):
    client, _ = gateway
    key_id, token, _ = issue(client)
    assert authenticate(token) == key_id
    client.post(f"/admin/api/keys/{key_id}/revoke")
    with pytest.raises(ProviderError, match="revoked_api_key"):
        reserve(key_id, REQUEST)


@pytest.mark.parametrize("dispatched", [True, False])
def test_expired_lease_releases_resources_without_future_requests(gateway, dispatched):
    client, factory = gateway
    key_id, _, _ = issue(client, max_concurrent_requests=1)
    admission = reserve(key_id, REQUEST)
    if dispatched:
        mark_dispatched(admission.id)
    with factory.begin() as db:
        db.get(Usage, admission.id).lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert reap_expired() == 1
    assert reap_expired() == 0
    with factory() as db:
        usage = db.get(Usage, admission.id)
        key = db.get(ApiKey, key_id)
        assert usage.status == "expired"
        assert usage.usage_estimated is dispatched
        assert key.reserved_usd == 0
        assert usage.accounted_tokens == (usage.reserved_tokens if dispatched else 0)
    assert reserve(key_id, REQUEST)


def test_heartbeat_and_idempotent_finalization(gateway, monkeypatch):
    client, factory = gateway
    at = datetime.now(UTC)
    monkeypatch.setattr("app.gateway.quota.utcnow", lambda: at)
    key_id, _, _ = issue(client)
    admission = reserve(key_id, REQUEST)
    mark_dispatched(admission.id)
    with factory() as db:
        initial_expiry = db.get(Usage, admission.id).lease_expires_at
    at += timedelta(seconds=40)
    assert renew(admission.id)
    with factory() as db:
        assert db.get(Usage, admission.id).lease_expires_at > initial_expiry
    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(lambda _: settle(admission.id, ACTUAL, 1), range(6)))
    assert outcomes.count(True) == 1
    assert not renew(admission.id)
    with factory() as db:
        assert db.get(ApiKey, key_id).reserved_usd == 0
        assert db.get(Usage, admission.id).accounted_tokens == 6


def test_known_provider_failure_frees_tokens_and_concurrency(gateway, monkeypatch):
    client, factory = gateway

    class Rejected(FakeChat):
        def events(self, call):
            raise ProviderError("provider_unavailable", 503, usage_unknown=False)
            yield

    monkeypatch.setitem(registry.providers, "bedrock", Rejected())
    key_id, _, headers = issue(client, max_concurrent_requests=1, tpm=4000)
    for _ in range(2):
        assert completion(client, headers).status_code == 503
    with factory() as db:
        key = db.get(ApiKey, key_id)
        assert key.reserved_usd == 0 and key.spent_usd == 0
        assert all(
            u.accounted_tokens == 0 and u.status == "failed" for u in db.scalars(select(Usage))
        )
    assert client.get("/admin/api/overview").json()["stats"]["active_requests"] == 0


def test_optional_budget_and_quota_editing(gateway):
    client, factory = gateway
    key_id, _, headers = issue(client, budget_usd=None, tokens_per_month=10000)
    assert completion(client, headers).status_code == 200
    response = client.put(
        f"/admin/api/keys/{key_id}",
        json={"name": "updated", "requests_per_day": 2, "max_concurrent_requests": 1},
    )
    assert response.status_code == 200
    with factory() as db:
        key = db.get(ApiKey, key_id)
        assert key.budget_usd is None
        assert key.tokens_per_month == 10000
        assert key.requests_per_day == 2
        assert key.max_concurrent_requests == 1
    assert completion(client, headers).status_code == 200
    assert completion(client, headers).status_code == 429


def test_late_usage_reconciles_an_expired_lease_atomically(gateway):
    client, factory = gateway
    key_id, _, _ = issue(client)
    admission = reserve(key_id, REQUEST)
    mark_dispatched(admission.id)
    with factory.begin() as db:
        row = db.get(Usage, admission.id)
        row.created_at -= timedelta(days=40)
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert reap_expired() == 1
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda _: settle(admission.id, ACTUAL, 10), range(5)))
    assert results.count(True) == 1
    with factory() as db:
        row = db.get(Usage, admission.id)
        key = db.get(ApiKey, key_id)
        assert row.accounted_tokens == 6 and not row.usage_estimated
        assert row.status == "completed"
        assert key.reserved_usd == 0 and key.spent_usd == row.cost_usd
