from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.providers.base import ProviderError


@lru_cache
def session_factory():
    value = get_settings().database_url
    if not value:
        raise ProviderError("database_not_configured", 503)
    url = value.get_secret_value()
    if not url.startswith("postgresql+psycopg://"):
        raise ProviderError("postgresql_required", 503)
    engine = create_engine(url, pool_pre_ping=True, echo=False, hide_parameters=True,
                           connect_args={"connect_timeout": 5})
    return sessionmaker(engine, expire_on_commit=False)
