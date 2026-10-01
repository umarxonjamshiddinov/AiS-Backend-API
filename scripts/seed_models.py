"""Insert all 42 catalog models; existing PostgreSQL configuration always wins."""

from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert

from app.catalog import bootstrap_values, load_bootstrap_catalog
from app.db.models import KeyModel, ModelRoute, Usage
from app.db.session import session_factory


def seed_models(factory=None):
    factory = factory or session_factory()
    inserted = 0
    with factory.begin() as db:
        legacy = db.scalar(
            select(ModelRoute)
            .where(
                ModelRoute.alias == "gpt-4o",
                ModelRoute.provider == "openai",
                ModelRoute.upstream_model == "gpt-4o",
                ModelRoute.enabled.is_(False),
            )
            .with_for_update()
        )
        if legacy is not None:
            permitted = db.scalar(select(exists().where(KeyModel.alias == legacy.alias)))
            used = db.scalar(select(exists().where(Usage.alias == legacy.alias)))
            if not permitted and not used:
                db.delete(legacy)
        for model in load_bootstrap_catalog():
            values = bootstrap_values(model)
            existing = db.get(ModelRoute, values["alias"])
            if existing is not None:
                if existing.model_key != model["model_key"]:
                    raise ValueError("Catalog model ID conflicts with an existing route")
                continue
            result = db.execute(
                insert(ModelRoute).values(**values).on_conflict_do_nothing(index_elements=["alias"])
            )
            inserted += result.rowcount
    return {"catalog_total": 42, "catalog_public": 40, "catalog_internal": 2, "inserted": inserted}


if __name__ == "__main__":
    import json

    print(json.dumps(seed_models()))
