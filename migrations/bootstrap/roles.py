"""Create the three roles and apply the Day 1 grant block.

Isolation runs both ways (v1.2 §3.9): the detector role cannot read scenario
state, and the scenario role cannot read telemetry. The protection is that the
grant is never made; the REVOKEs are declared intent and defence in depth.

No `GRANT ... ON ALL TABLES` is issued anywhere.
"""

import os

from psycopg import sql

from bootstrap._conn import connect

ROLES = {
    "blastradius_app": "BLASTRADIUS_APP_PASSWORD",
    "blastradius_detector": "BLASTRADIUS_DETECTOR_PASSWORD",
    "blastradius_scenario": "BLASTRADIUS_SCENARIO_PASSWORD",
}

GRANTS = """
GRANT SELECT, INSERT, UPDATE, DELETE ON span, trace TO blastradius_detector;
GRANT SELECT, INSERT, UPDATE, DELETE ON incident, incident_symptom TO blastradius_detector;
GRANT SELECT ON slo TO blastradius_detector;
GRANT SELECT, UPDATE ON ingest_state TO blastradius_detector;
GRANT SELECT ON service, domain, domain_edge TO blastradius_detector;
REVOKE ALL ON ground_truth, scenario_run, "order" FROM blastradius_detector;

GRANT SELECT, INSERT, DELETE ON "order" TO blastradius_app;
GRANT SELECT ON service, domain TO blastradius_app;
REVOKE ALL ON span, trace, ground_truth, scenario_run FROM blastradius_app;
REVOKE ALL ON incident, incident_symptom, slo FROM blastradius_app;

GRANT SELECT, INSERT, UPDATE, DELETE ON scenario_run, ground_truth TO blastradius_scenario;
GRANT SELECT ON service, domain TO blastradius_scenario;
REVOKE ALL ON span, trace, "order" FROM blastradius_scenario;
REVOKE ALL ON incident, incident_symptom FROM blastradius_scenario;
"""


def main() -> None:
    with connect() as conn, conn.cursor() as cur:
        for role, env_key in ROLES.items():
            password = os.environ[env_key]
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            stmt = "ALTER ROLE {} WITH LOGIN PASSWORD {}" if cur.fetchone() else \
                   "CREATE ROLE {} WITH LOGIN PASSWORD {}"
            # Identifier/Literal quoting; the password never reaches a log line.
            cur.execute(
                sql.SQL(stmt).format(sql.Identifier(role), sql.Literal(password))
            )
            cur.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role))
            )
            print(f"role {role}: ready")

        cur.execute(GRANTS)
        print("grants applied")


if __name__ == "__main__":
    main()
