import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import completion, issue
from sqlalchemy import select

from app.db.models import ApiKey, KeyModel, Usage


@pytest.fixture
def archived_case(gateway):
    client, factory = gateway
    kept_id, _, kept_headers = issue(client, name="Active control")
    key_id, secret, headers = issue(client, name="Archive candidate", rpm=None, tpm=None)
    issue(client, name="Disabled control", active=False)
    issue(client, name="Expired control", expires_at="2020-01-01T00:00:00Z")
    user = client.post("/admin/api/users", json={"username": "inactive-owner"}).json()
    issue(client, name="Inactive owner control", user_id=user["id"])
    client.put("/admin/api/users/" + user["id"], json={"active": False})
    assert completion(client, headers).status_code == 200
    before = client.get("/admin/api/overview").json()
    with factory() as db:
        key = db.get(ApiKey, key_id)
        saved = {c.name: getattr(key, c.name) for c in ApiKey.__table__.columns}
        usage = db.scalar(select(Usage).where(Usage.key_id == key_id))
        saved_usage = {c.name: getattr(usage, c.name) for c in Usage.__table__.columns}
    assert client.post(f"/admin/api/keys/{key_id}/revoke").status_code == 200
    after = client.get("/admin/api/overview").json()
    return (
        client,
        factory,
        {
            "before": before,
            "after": after,
            "key_id": key_id,
            "kept_id": kept_id,
            "kept_headers": kept_headers,
            "headers": headers,
            "secret": secret,
            "saved": saved,
            "saved_usage": saved_usage,
        },
    )


def test_revoke_preserves_archived_key_metadata_permissions_usage_and_auth(archived_case):
    client, factory, case = archived_case
    with factory() as db:
        key = db.get(ApiKey, case["key_id"])
        assert key is not None and not key.active and key.revoked_at is not None
        for field, value in case["saved"].items():
            if field not in ("active", "revoked_at"):
                assert getattr(key, field) == value
        assert db.get(KeyModel, (key.id, "ais-fast")) is not None
        usage = db.get(Usage, case["saved_usage"]["id"])
        assert {c.name: getattr(usage, c.name) for c in Usage.__table__.columns} == case[
            "saved_usage"
        ]
    assert case["saved_usage"]["id"] in {u["id"] for u in case["after"]["usage"]}
    assert case["secret"] not in json.dumps(case["after"])
    denied = completion(client, case["headers"])
    assert denied.status_code == 401 and denied.json()["error"]["code"] == "revoked_api_key"
    assert completion(client, case["kept_headers"]).status_code == 200


def test_admin_moves_revoked_keys_to_archive_and_counts_only_active(archived_case):
    _, _, case = archived_case
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute the admin UI regression test")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, str(root / "tests/admin_key_archive.cjs")],
        input=json.dumps(
            {"before": case["before"], "after": case["after"], "key_id": case["key_id"]}
        ),
        text=True,
        encoding="utf-8",
        capture_output=True,
        cwd=root,
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert result.returncode == 0, result.stderr
