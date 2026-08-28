"""Day 1 telemetry spine schema.

Technical Contract v1.2 §3, restricted to the Day 1 subset. Scenario tables are
created but unused -- the detector grant test is a Day 1 acceptance criterion and
needs a real table to be denied on.

Revision ID: 0001
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = """
CREATE TYPE service_kind    AS ENUM ('process');
CREATE TYPE domain_kind     AS ENUM ('process','logical_dependency','datastore');
CREATE TYPE span_kind       AS ENUM ('INTERNAL','CLIENT','SERVER');
CREATE TYPE span_status     AS ENUM ('OK','ERROR');
CREATE TYPE checkout_status AS ENUM ('CONFIRMED','FAILED');
CREATE TYPE scenario_state  AS ENUM ('IDLE','ARMED','INJECTING','ACTIVE',
                                     'RECOVERING','COMPLETE','REVEALED');

CREATE TABLE service (
  id smallint PRIMARY KEY,
  name text NOT NULL UNIQUE,
  kind service_kind NOT NULL DEFAULT 'process'
);

CREATE TABLE domain (
  id smallint PRIMARY KEY,
  name text NOT NULL UNIQUE,
  kind domain_kind NOT NULL,
  display_order smallint NOT NULL DEFAULT 0
);

CREATE TABLE domain_edge (
  caller_domain_id smallint NOT NULL REFERENCES domain(id),
  callee_domain_id smallint NOT NULL REFERENCES domain(id),
  PRIMARY KEY (caller_domain_id, callee_domain_id)
);

CREATE TABLE span (
  trace_id              char(32) NOT NULL,
  span_id               char(16) NOT NULL,
  parent_span_id        char(16),
  emitting_service_id   smallint NOT NULL REFERENCES service(id),
  attribution_domain_id smallint NOT NULL REFERENCES domain(id),
  span_kind             span_kind NOT NULL,
  operation             text NOT NULL,
  start_ts              timestamptz NOT NULL,
  end_ts                timestamptz NOT NULL,
  duration_ms           integer NOT NULL,
  status                span_status NOT NULL,
  blocking              boolean NOT NULL DEFAULT true,
  attributes            jsonb NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (trace_id, span_id)
);
CREATE INDEX span_trace_idx  ON span (trace_id);
CREATE INDEX span_domain_idx ON span (attribution_domain_id, start_ts DESC);
CREATE INDEX span_start_idx  ON span (start_ts DESC);

CREATE TABLE trace (
  trace_id         char(32) PRIMARY KEY,
  root_span_id     char(16),
  root_operation   text,
  root_status      span_status,
  root_start_ts    timestamptz,
  root_end_ts      timestamptz,
  root_duration_ms integer,
  order_id         uuid,
  channel          text,
  has_promo        boolean,
  payment_method   text,
  checkout_status  checkout_status,
  span_count       integer NOT NULL DEFAULT 0,
  last_span_ts     timestamptz NOT NULL
);
CREATE INDEX trace_root_end_idx ON trace (root_end_ts DESC) WHERE root_span_id IS NOT NULL;
CREATE INDEX trace_cohort_idx   ON trace (root_end_ts DESC, channel, has_promo, payment_method);

CREATE TABLE "order" (
  id uuid PRIMARY KEY,
  trace_id char(32) NOT NULL,
  channel text NOT NULL,
  has_promo boolean NOT NULL,
  payment_method text NOT NULL,
  status checkout_status NOT NULL,
  created_ts timestamptz NOT NULL
);

CREATE TABLE ingest_state (
  id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  last_reset_ts timestamptz NOT NULL DEFAULT '-infinity'
);

CREATE TABLE scenario_run (
  id uuid PRIMARY KEY,
  state scenario_state NOT NULL,
  mode text NOT NULL,
  profile text NOT NULL,
  seed bigint NOT NULL,
  scenario text NOT NULL,
  started_ts timestamptz NOT NULL,
  ended_ts timestamptz,
  revealed_ts timestamptz
);

CREATE TABLE ground_truth (
  scenario_run_id uuid PRIMARY KEY REFERENCES scenario_run(id),
  injected_domain_id smallint NOT NULL REFERENCES domain(id),
  fault_type text NOT NULL,
  started_ts timestamptz NOT NULL,
  ended_ts timestamptz
);
"""

DROP = """
DROP TABLE IF EXISTS ground_truth, scenario_run, ingest_state, "order",
                     trace, span, domain_edge, domain, service CASCADE;
DROP TYPE IF EXISTS scenario_state, checkout_status, span_status, span_kind,
                    domain_kind, service_kind;
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    op.execute(DROP)
