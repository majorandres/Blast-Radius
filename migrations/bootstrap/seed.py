"""Seed reference data (v1.2 §3.2) and the single ingest_state row (§7.4).

`service` and `domain` share names where a process is also a domain. They are
separate tables and must be joined by explicit id, never by name.
"""

from bootstrap._conn import connect

SEED = """
INSERT INTO service (id, name, kind) VALUES
  (1, 'ordering-app', 'process'),
  (2, 'promo-provider', 'process')
ON CONFLICT (id) DO NOTHING;

INSERT INTO domain (id, name, kind, display_order) VALUES
  (1, 'ordering-app',    'process',            0),
  (2, 'promo-provider',  'process',            1),
  (3, 'payment-gateway', 'logical_dependency', 2),
  (4, 'order-datastore', 'datastore',          3)
ON CONFLICT (id) DO NOTHING;

INSERT INTO domain_edge VALUES (1,2), (1,3), (1,4)
ON CONFLICT DO NOTHING;

INSERT INTO ingest_state (id) VALUES (1)
ON CONFLICT (id) DO NOTHING;
"""


def main() -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(SEED)
        for table in ("service", "domain", "domain_edge", "ingest_state"):
            cur.execute(f"SELECT count(*) FROM {table}")
            print(f"seed {table}: {cur.fetchone()[0]} rows")


if __name__ == "__main__":
    main()
