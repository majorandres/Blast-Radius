"""The reveal table (v1.2 §3.8).

`reveal.incident_id` is deliberately **not** a foreign key. A FK would require
the scenario role to hold a reference on a detector-owned table, creating
schema-level coupling that contradicts §1.3. The scenario role cannot read
`incident` at all, so it stores the id as an opaque value and validates the
association over the same public HTTP endpoint the frontend uses.

`incident_first_breach_ts` is recorded so the FINAL-05 window validation is
auditable after the fact: you can see which incident was scored and why it was
accepted.

Revision ID: 0003
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SCHEMA = """
CREATE TABLE reveal (
  scenario_run_id uuid PRIMARY KEY REFERENCES scenario_run(id),
  incident_id uuid,
  incident_first_breach_ts timestamptz,
  detected_domain text,
  detected_verdict text,
  correct boolean NOT NULL,
  revealed_ts timestamptz NOT NULL
);
"""

DROP = "DROP TABLE IF EXISTS reveal CASCADE;"


def upgrade() -> None:
    op.execute(SCHEMA)
    # Grants live in bootstrap/roles.py: the roles are created after the
    # migrations run, so a GRANT here would name a role that does not exist.


def downgrade() -> None:
    op.execute(DROP)
