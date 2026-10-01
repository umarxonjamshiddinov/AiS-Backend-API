from decimal import Decimal

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import ApiKey, Base, ModelRoute, Usage, User
from app.gateway.quota import reap_expired


def migration_config(connection):
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


def test_upgrade_preserves_existing_gateway_data(isolated_database, monkeypatch):
    with isolated_database.begin() as connection:
        config = migration_config(connection)
        command.upgrade(config, "0001")
        connection.execute(
            text(
                "INSERT INTO model_routes VALUES "
                "('ais-fast','bedrock','amazon.nova-micro-v1:0',0.035,0.14,4096,true),"
                "('gpt-4o','openai','gpt-4o',2.5,10,4096,false)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO api_keys (id,name,prefix,key_hash,active,budget_usd,spent_usd,"
                "reserved_usd,rpm,tpm,created_at) VALUES "
                "('key-1','existing','sk-myais-test','unit-hash',true,10,1,0.2,30,100000,now()),"
                "('key-2','revoked','sk-myais-old','other-unit-hash',false,10,0,0,30,100000,now())"
            )
        )
        connection.execute(text("INSERT INTO key_models VALUES ('key-1','ais-fast')"))
        connection.execute(
            text(
                "INSERT INTO usage (id,key_id,alias,created_at,status,"
                "reserved_tokens,reserved_cost,"
                "input_price,output_price,input_tokens,output_tokens,cost_usd,latency_ms) VALUES "
                "('u-1','key-1','ais-fast',now(),'completed',100,0.01,0.035,0.14,5,7,1,20),"
                "('u-2','key-1','ais-fast',now(),'uncertain',200,0.2,0.035,0.14,0,0,0,30)"
            )
        )
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0005"
        assert connection.scalar(text("SELECT count(*) FROM key_models")) == 1
        with Session(connection) as db:
            route = db.get(ModelRoute, "ais-fast")
            assert route.upstream_model == "amazon.nova-micro-v1:0" and route.enabled
            assert route.external_api_allowed and route.capabilities["tools"]
            assert not route.capabilities.get("vision", False)
            assert db.get(ModelRoute, "gpt-4o").provider_transport == "openai_chat"
            key = db.get(ApiKey, "key-1")
            assert key.user_id == "00000000-0000-4000-8000-000000000001"
            assert db.get(User, key.user_id).active
            assert key.key_hash == "unit-hash" and key.budget_usd == 10
            assert key.rpm == 30 and key.tpm == 100000
            assert key.spent_usd == 1 and key.reserved_usd == Decimal("0.2")
            assert key.tokens_per_month is None and key.max_concurrent_requests is None
            assert db.get(ApiKey, "key-2").revoked_at is not None
            assert db.get(Usage, "u-1").accounted_tokens == 12
            assert db.get(Usage, "u-1").provider == "bedrock"
            assert db.get(Usage, "u-1").user_id == key.user_id
            assert db.get(Usage, "u-2").status == "reserved"
        differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
        assert differences == []
    factory = sessionmaker(isolated_database, expire_on_commit=False)
    monkeypatch.setattr("app.gateway.quota.session_factory", lambda: factory)
    assert reap_expired() == 1
    with factory() as db:
        assert db.get(ApiKey, "key-1").reserved_usd == 0
        assert db.get(ApiKey, "key-1").spent_usd == Decimal("1.2")
        assert db.get(Usage, "u-2").status == "expired"


def test_fresh_upgrade_and_roundtrip(isolated_database):
    with isolated_database.begin() as connection:
        config = migration_config(connection)
        command.upgrade(config, "head")
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
        command.downgrade(config, "0001")
        command.upgrade(config, "head")
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0005"
        with Session(connection) as db:
            assert list(db.scalars(select(ApiKey))) == []


@pytest.mark.parametrize("rpm,tpm", [(None, 100000), (30, None), (None, None)])
def test_nullable_rate_migration_preserves_limits_and_guards_downgrade(isolated_database, rpm, tpm):
    with isolated_database.begin() as connection:
        config = migration_config(connection)
        command.upgrade(config, "0003")
        connection.execute(
            text(
                "INSERT INTO api_keys (id,name,prefix,key_hash,active,spent_usd,reserved_usd,"
                "rpm,tpm,created_at) VALUES "
                "('old-rate-key','existing','sk-myais-test','rate-test-hash',true,0,0,30,100000,now())"
            )
        )
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        row = connection.execute(text("SELECT rpm,tpm,key_hash FROM api_keys")).one()
        assert tuple(row) == (30, 100000, "rate-test-hash")
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
        connection.execute(text("UPDATE api_keys SET rpm=:rpm,tpm=:tpm"), {"rpm": rpm, "tpm": tpm})
        assert tuple(connection.execute(text("SELECT rpm,tpm FROM api_keys")).one()) == (rpm, tpm)
        with pytest.raises(RuntimeError, match="Set explicit RPM and TPM"):
            command.downgrade(config, "0003")
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0004"
        connection.execute(text("UPDATE api_keys SET rpm=7,tpm=12345"))
        command.downgrade(config, "0003")
        assert tuple(connection.execute(text("SELECT rpm,tpm FROM api_keys")).one()) == (7, 12345)
        command.upgrade(config, "head")
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
