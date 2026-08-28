import os

import psycopg


def connect() -> psycopg.Connection:
    """Superuser connection. Migrations run as the table owner so the three
    application roles never own anything (v1.2 §3.9)."""
    url = os.environ["DATABASE_URL_MIGRATE"]
    for prefix in ("postgresql+psycopg://", "postgresql+asyncpg://"):
        url = url.replace(prefix, "postgresql://")
    return psycopg.connect(url, autocommit=True)
