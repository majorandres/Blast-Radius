import os

from alembic import context
from sqlalchemy import create_engine


def _url() -> str:
    url = os.environ["DATABASE_URL_MIGRATE"]
    # Alembic runs synchronously on psycopg3. Normalise whatever driver the
    # compose env supplies, including the bare `postgresql://` form, which
    # SQLAlchemy would otherwise resolve to psycopg2.
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg2://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def run_migrations_online() -> None:
    engine = create_engine(_url(), future=True)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
