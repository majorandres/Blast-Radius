"""Detection schema: SLOs, incidents, symptoms.

Technical Contract v1.2 §3.1, §3.6, §3.7.

`slo` carries only what is genuinely per-SLO. Window length and the sample floor
come from the active timing profile instead, so switching PROFILE compresses the
observation windows without touching a threshold or requiring a data migration.
That is what makes the README's claim -- detection logic and thresholds are
identical in both profiles -- literally true rather than merely intended.

`incident` has no scenario reference of any kind, and never will. Tested.

Revision ID: 0002
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SCHEMA = """
CREATE TYPE incident_state        AS ENUM ('PENDING','OPEN','RECOVERING','CLOSED');
CREATE TYPE attribution_verdict   AS ENUM ('ATTRIBUTED','AMBIGUOUS','NO_DIAGNOSIS');
CREATE TYPE impact_verdict        AS ENUM ('AFFECTED','DEGRADED','UNAFFECTED','INSUFFICIENT_DATA');
CREATE TYPE concentration_verdict AS ENUM ('CONCENTRATED','PROPORTIONAL','SPARED','INSUFFICIENT_DATA');

CREATE TABLE slo (
  id smallint PRIMARY KEY,
  name text NOT NULL UNIQUE,
  metric text NOT NULL,
  comparator text NOT NULL,
  threshold numeric NOT NULL
);

CREATE TABLE incident (
  id uuid PRIMARY KEY,
  state incident_state NOT NULL,
  first_breach_ts timestamptz NOT NULL,
  opened_ts timestamptz,
  closed_ts timestamptz,
  severity text NOT NULL,
  verdict attribution_verdict,
  attributed_domain_id smallint REFERENCES domain(id),
  attribution_share numeric(5,4),
  candidate_trace_count integer,
  attribution_detail jsonb NOT NULL DEFAULT '{}'::jsonb,
  baseline_snapshot  jsonb NOT NULL DEFAULT '{}'::jsonb,
  impact             jsonb NOT NULL DEFAULT '{}'::jsonb,
  concentration      jsonb NOT NULL DEFAULT '{}'::jsonb,
  primary_dimension  text,
  primary_cohort     text,
  narrative jsonb,
  narrative_source text
);
CREATE INDEX incident_state_idx ON incident (state, first_breach_ts DESC);

CREATE TABLE incident_symptom (
  incident_id uuid NOT NULL REFERENCES incident(id) ON DELETE CASCADE,
  slo_id smallint NOT NULL REFERENCES slo(id),
  breached_ts timestamptz NOT NULL,
  observed_value numeric NOT NULL,
  PRIMARY KEY (incident_id, slo_id, breached_ts)
);
"""

# v1.2 §11. error_rate was removed in v1.1 as an exact mirror of checkout_success.
# These two are genuinely independent: Scenario C breaches p95_latency while
# checkout_success holds, which is the whole reason the pair exists.
SEED = """
INSERT INTO slo (id, name, metric, comparator, threshold) VALUES
  (1, 'checkout_success', 'confirmed_ratio', 'gte', 0.98),
  (2, 'p95_latency',      'root_duration_p95_ms', 'lte', 1000)
ON CONFLICT (id) DO NOTHING;
"""

DROP = """
DROP TABLE IF EXISTS incident_symptom, incident, slo CASCADE;
DROP TYPE IF EXISTS concentration_verdict, impact_verdict, attribution_verdict, incident_state;
"""


def upgrade() -> None:
    op.execute(SCHEMA)
    op.execute(SEED)
    # Grants live in bootstrap/roles.py: the roles are created after the
    # migrations run, so a GRANT here would name a role that does not exist.


def downgrade() -> None:
    op.execute(DROP)
