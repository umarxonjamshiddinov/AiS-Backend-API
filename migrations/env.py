from alembic import context
from sqlalchemy import create_engine, pool

from app.core.config import get_settings
from app.db.models import Base

settings = get_settings()
if not settings.database_url:
    raise RuntimeError("DATABASE_URL is required")
url = settings.database_url.get_secret_value()
if not url.startswith("postgresql+psycopg://"):
    raise RuntimeError("PostgreSQL is required")

if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
elif context.config.attributes.get("connection") is not None:
    # Explicit connections let migration tests use an isolated ais_test schema.
    connection = context.config.attributes["connection"]
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(
        url, poolclass=pool.NullPool, hide_parameters=True, connect_args={"connect_timeout": 5}
    )
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
