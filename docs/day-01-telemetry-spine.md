# Day 1 — Telemetry Spine

> **Source of truth:** Technical Contract v1.2 (frozen). This document is the Day 1 working slice of it, with the relevant specs inlined so you don't have to cross-reference while coding. Where this document and v1.2 disagree, v1.2 wins — and tell me, because that's a contract bug.
>
> **Suggested location:** `docs/day-01-telemetry-spine.md`, alongside `docs/TECHNICAL-CONTRACT-v1.2.md`.

---

## Goal

A synthetic checkout produces a hierarchical OpenTelemetry trace spanning `ordering-app` and `promo-provider`, and that same trace is persisted correctly in PostgreSQL and visible in Jaeger.

Nothing detects anything today. No SLOs, no incidents, no attribution.

---

## Gate zero — do this before anything else

Stand up the two FastAPI processes with OTel and confirm in Jaeger that a `promo.apply` span in `ordering-app` and a `promo.handle` span in `promo-provider` share **one trace ID**.

If `traceparent` does not propagate, promo spans arrive as orphan roots, and every attribution result on Day 2 will be wrong in a way that looks like an algorithm bug. Fix this before writing the exporter, the schema, or anything else.

Minimum to prove it: `opentelemetry-instrumentation-httpx` on the client side, `opentelemetry-instrumentation-fastapi` on the server side, both exporting OTLP to Jaeger. Open `localhost:16686`, find the trace, confirm two services and one trace ID.

---

## In scope

Repo foundation · Docker Compose (postgres, migrate, jaeger, observability-service, promo-provider, ordering-app) · Alembic + Day 1 schema + roles and grants + seed · ordering-app checkout with the full span tree · promo-provider with a real HTTP boundary · seeded traffic generator · dual-export OTel on both processes · `BlastRadiusSpanExporter` · observability-service ingest only.

## Out of scope

Scenario controller behavior · fault injection · SLO engine · incidents · attribution · blast radius · frontend dashboard · LLM · runbooks · Scenarios A/B/C · deployment.

Faults are structurally scaffolded (the `PUT /_faults` endpoint may exist) but **all fault behavior is disabled**. The frontend may exist as a bare Vite scaffold and nothing more.

Scenario tables are created and the isolation model is enforced even though nothing writes to them yet. That is deliberate — the grant test is a Day 1 acceptance criterion.

---

## Repository layout (Day 1 only)

Create only what Day 1 needs. No speculative modules for later days.

```
blast-radius/
├── apps/
│   ├── ordering_app/
│   │   ├── app/
│   │   │   ├── main.py
│   │   │   ├── config.py
│   │   │   ├── checkout.py
│   │   │   ├── dependencies/
│   │   │   │   ├── promo_client.py
│   │   │   │   ├── payment.py
│   │   │   │   └── db.py
│   │   │   ├── telemetry/
│   │   │   │   ├── setup.py
│   │   │   │   ├── exporter.py
│   │   │   │   └── attributes.py
│   │   │   ├── traffic/generator.py
│   │   │   └── faults.py          (scaffold, disabled)
│   │   ├── tests/
│   │   ├── Dockerfile
│   │   └── pyproject.toml
│   ├── promo_provider/
│   │   ├── app/{main,config,faults}.py
│   │   ├── app/telemetry/{setup,exporter}.py
│   │   ├── tests/
│   │   ├── Dockerfile
│   │   └── pyproject.toml
│   ├── observability_service/
│   │   ├── app/
│   │   │   ├── main.py
│   │   │   ├── config.py
│   │   │   ├── db.py
│   │   │   └── ingest/
│   │   │       ├── api.py
│   │   │       ├── writer.py
│   │   │       └── fence.py
│   │   ├── tests/
│   │   ├── Dockerfile
│   │   └── pyproject.toml
│   └── frontend/                  (bare Vite scaffold only)
├── packages/contracts/
│   └── blastradius_contracts/
│       ├── __init__.py
│       └── telemetry.py           SpanEnvelope, SpanBatch
├── migrations/                    Alembic
├── docs/
│   ├── TECHNICAL-CONTRACT-v1.2.md
│   └── day-01-telemetry-spine.md
├── docker-compose.yml
├── .env.example
└── README.md
```

`packages/contracts` exists on Day 1 because `SpanEnvelope` crosses three processes. Nothing else is shared yet.

---

## Schema — Day 1 subset

Executable as written. Scenario tables are created but unused.

```sql
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

-- created but unused on Day 1; required for the grant test
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
```

Root columns on `trace` are nullable because children routinely arrive before the root. A trace with `root_span_id IS NULL` is invisible to every query.

### Roles and grants

```sql
GRANT SELECT, INSERT, UPDATE, DELETE ON span, trace TO blastradius_detector;
GRANT SELECT, UPDATE ON ingest_state TO blastradius_detector;
GRANT SELECT ON service, domain, domain_edge TO blastradius_detector;
REVOKE ALL ON ground_truth, scenario_run, "order" FROM blastradius_detector;

GRANT SELECT, INSERT, DELETE ON "order" TO blastradius_app;
GRANT SELECT ON service, domain TO blastradius_app;
REVOKE ALL ON span, trace, ground_truth, scenario_run FROM blastradius_app;

GRANT SELECT, INSERT, UPDATE, DELETE ON scenario_run, ground_truth TO blastradius_scenario;
GRANT SELECT ON service, domain TO blastradius_scenario;
REVOKE ALL ON span, trace, "order" FROM blastradius_scenario;
```

Isolation runs both ways. The scenario role cannot read telemetry either.

### Seed data

```sql
INSERT INTO service (id, name, kind) VALUES
  (1, 'ordering-app', 'process'),
  (2, 'promo-provider', 'process');

INSERT INTO domain (id, name, kind, display_order) VALUES
  (1, 'ordering-app',    'process',            0),
  (2, 'promo-provider',  'process',            1),
  (3, 'payment-gateway', 'logical_dependency', 2),
  (4, 'order-datastore', 'datastore',          3);

INSERT INTO domain_edge VALUES (1,2), (1,3), (1,4);

INSERT INTO ingest_state (id) VALUES (1);
```

`service` and `domain` share names where a process is also a domain. They are separate tables and must be joined by explicit id, never by name.

---

## Span tree

| Operation | Emitting service | Kind | Attribution domain | Blocking |
|---|---|---|---|---|
| `checkout` | ordering-app | SERVER | ordering-app | yes |
| `validate_order` | ordering-app | INTERNAL | ordering-app | yes |
| `pricing` | ordering-app | INTERNAL | ordering-app | yes |
| `loyalty_tier_lookup` | ordering-app | INTERNAL | ordering-app | yes |
| `db.pool_acquire` | ordering-app | CLIENT | order-datastore | yes |
| `promo.apply` | ordering-app | CLIENT | promo-provider | yes |
| `promo.handle` | promo-provider | SERVER | promo-provider | yes |
| `payment.authorize` | ordering-app | CLIENT | payment-gateway | yes |
| `db.persist_order` | ordering-app | CLIENT | order-datastore | yes |
| `analytics.publish` | ordering-app | INTERNAL | ordering-app | **no** |
| `confirmation` | ordering-app | INTERNAL | ordering-app | yes |

`loyalty_tier_lookup` is a child of `pricing`. `promo.handle` is a child of `promo.apply` via trace context. Everything else is a direct child of `checkout`. `promo.apply` only exists when `has_promo` is true.

**Two identities per span.** `emitting_service` comes from the OTel `Resource(service.name)` and is truthful — only `ordering-app` and `promo-provider` exist as processes. `attribution_domain` is the span attribute `blastradius.domain` and is logical. A CLIENT span's attribution domain is always its **peer**, never its emitter. That is why `payment.authorize` and `db.pool_acquire` carry domains that are not processes.

Do not create fake payment or datastore services in Jaeger's resource topology. Jaeger must show two services.

### Attributes

Root `checkout` span only: `order.id`, `order.channel`, `order.has_promo`, `order.payment_method`.
Every span: `blastradius.domain`, `blastradius.blocking`.

Put every attribute key in `telemetry/attributes.py` as a constant, mirrored in the observability service. Never inline a literal — Day 2's detection logic depends on these strings.

---

## SpanEnvelope

```python
class SpanEnvelope(BaseModel):
    trace_id: str
    span_id: str
    parent_span_id: str | None
    emitting_service: str            # OTel resource name
    attribution_domain: str          # blastradius.domain
    span_kind: Literal["INTERNAL","CLIENT","SERVER"]
    operation: str
    start_unix_nano: int
    end_unix_nano: int
    status: Literal["OK","ERROR"]
    blocking: bool = True
    attributes: dict[str, str | int | float | bool]

class SpanBatch(BaseModel):
    spans: list[SpanEnvelope] = Field(max_length=500)
```

---

## Dual export

Both emitting processes get two `BatchSpanProcessor`s on one `TracerProvider`:

```
TracerProvider(Resource(service.name=<process>))
├── BatchSpanProcessor → OTLPSpanExporter(http)     → jaeger:4318
└── BatchSpanProcessor → BlastRadiusSpanExporter    → observability:8004/internal/spans
```

Both: `max_export_batch_size=200`, `schedule_delay_millis=2000`.

`BlastRadiusSpanExporter` subclasses the real OTel `SpanExporter`, maps `ReadableSpan` → `SpanEnvelope`, and POSTs `SpanBatch`. Retries 3 times at 200/400/800ms, then logs and returns `SpanExportResult.FAILURE`. Explicit HTTP timeout on every call.

Note for `DECISIONS.md`: the observability service is not an OTLP receiver. It consumes a custom JSON projection of spans emitted by a real SDK. Jaeger receives genuine OTLP. Say this plainly; don't let the README imply otherwise.

---

## Ingest

`POST /internal/spans` → `202`, body `{accepted, fenced}`.

### Fence

Load `ingest_state.last_reset_ts` into memory at startup and after any update. Drop spans whose `end_ts` precedes it before insert, count them, and report the count in the response and on `/healthz`.

**Invariant: no span whose `end_ts` precedes `last_reset_ts` is ever ingested.** Only the ingest-side half is built today; the reset workflow comes later.

### Persistence

```sql
WITH ins AS (
  INSERT INTO span (trace_id, span_id, parent_span_id, emitting_service_id,
                    attribution_domain_id, span_kind, operation,
                    start_ts, end_ts, duration_ms, status, blocking, attributes)
  VALUES %s
  ON CONFLICT (trace_id, span_id) DO NOTHING
  RETURNING trace_id
),
agg AS (
  SELECT trace_id, count(*) AS new_spans FROM ins GROUP BY trace_id
)
INSERT INTO trace (trace_id, span_count, last_span_ts)
SELECT trace_id, new_spans, now() FROM agg
ON CONFLICT (trace_id) DO UPDATE SET
  span_count   = trace.span_count + EXCLUDED.span_count,
  last_span_ts = EXCLUDED.last_span_ts;
```

Then, for any newly inserted root span:

```sql
UPDATE trace t SET
  root_span_id     = s.span_id,
  root_operation   = s.operation,
  root_status      = s.status,
  root_start_ts    = s.start_ts,
  root_end_ts      = s.end_ts,
  root_duration_ms = s.duration_ms,
  order_id         = (s.attributes->>'order.id')::uuid,
  channel          =  s.attributes->>'order.channel',
  has_promo        = (s.attributes->>'order.has_promo')::boolean,
  payment_method   =  s.attributes->>'order.payment_method',
  checkout_status  = CASE WHEN s.status = 'ERROR' THEN 'FAILED'::checkout_status
                          ELSE 'CONFIRMED'::checkout_status END
FROM span s
WHERE s.trace_id = t.trace_id
  AND s.parent_span_id IS NULL
  AND t.root_span_id IS NULL;
```

A fully duplicate batch produces zero rows in `agg`, so `last_span_ts` does not advance and the Day 2 settle clock is not delayed. This is the behavior test 5 checks.

Unknown `emitting_service` or `attribution_domain` → `400`. Malformed batch → logged and dropped, never a `500`.

---

## Traffic generator

Lives in `ordering-app`, started by lifespan, gated on `TRAFFIC_ENABLED`.

- 150 orders/min via exponential inter-arrival delays (Poisson), not a fixed sleep. A perfectly flat baseline makes p95 meaningless on Day 2.
- Semaphore of 40 in-flight checkouts. This also becomes the drain mechanism later, so derive in-flight count from it.
- Cohorts, independent draws: channel `mobile 55 / web 35 / aggregator 10`; `has_promo` 35%; payment `card 60 / wallet 25 / other 15`.
- `random.Random(TRAFFIC_SEED)` per process. Cohort sequence is reproducible; wall-clock timing is not, and that's fine.
- No ramps. Scenario traffic control is Day 2.

The DB pool is genuinely bounded from Day 1: `pool_size=10, max_overflow=0, pool_timeout=5`. `db.pool_acquire` wraps the real `engine.connect()` and records `db.pool.wait_ms`. Wait time is measured, never injected. Keep the traffic semaphore low enough that baseline traffic doesn't saturate the pool.

---

## Config (Day 1)

**Required:** `DATABASE_URL_APP`, `DATABASE_URL_DETECTOR`, `DATABASE_URL_SCENARIO`, `OBSERVABILITY_INGEST_URL`, `PROMO_PROVIDER_URL`.

**Optional:** `TRAFFIC_ENABLED=true`, `TRAFFIC_BASE_RATE_PER_MIN=150`, `TRAFFIC_SEED=42`, `TRAFFIC_MAX_CONCURRENCY=40`, `OTLP_ENDPOINT=http://jaeger:4318`, `DB_POOL_SIZE=10`, `DB_POOL_TIMEOUT=5`, `PROMO_CLIENT_TIMEOUT_MS=2000`.

`.env.example` committed, `.env` gitignored.

---

## Compose

| Service | Ports | Depends on (condition) |
|---|---|---|
| `postgres` | 5432 | — |
| `migrate` | — | postgres (healthy) |
| `jaeger` | 16686, 4318 | — |
| `observability-service` | 8004 | migrate (completed) |
| `promo-provider` | 8002 | — |
| `ordering-app` | 8001 | observability, promo, postgres (healthy) |

Healthchecks: `pg_isready` for Postgres, `GET /readyz` at 5s intervals with 12 retries for each app. `migrate` is one-shot: Alembic, then roles and grants, then seed.

---

## Suggested build order within the day

1. Compose skeleton with postgres and jaeger only. Confirm both come up.
2. Two bare FastAPI apps with OTel → Jaeger. **Gate zero: verify trace context propagates.**
3. Alembic, schema, roles, grants, seed. Confirm the grant test fails as expected.
4. Full checkout span tree with correct domains and blocking flags. Verify shape in Jaeger.
5. `SpanEnvelope`, `BlastRadiusSpanExporter`, ingest endpoint, persistence, trace head.
6. Fence.
7. Traffic generator.
8. Tests.

Do not write the exporter before step 2 passes.

---

## Tests

Real PostgreSQL wherever database behavior or grants matter. No mocked DB for tests 5–11.

1. Order → export → observability ingestion
2. Promo trace-context propagation (one trace ID across both processes)
3. Hierarchy and parent relationships correct
4. `emitting_service` distinct from `attribution_domain` — assert `payment.authorize` is `ordering-app` / `payment-gateway`
5. Duplicate batch: zero inserts, `span_count` unchanged, `last_span_ts` unchanged
6. Partial duplicate: head advances by new spans only
7. Children-before-root: head exists with null root fields, invisible to root-filtered queries
8. Trace-head root population when the root arrives last
9. Transaction dimensions copied into the head correctly
10. Ingest fence: span older than `last_reset_ts` rejected and counted
11. Detector role `SELECT * FROM ground_truth` raises `InsufficientPrivilege`

---

## Acceptance criteria

- [ ] `docker compose up` starts the Day 1 system on a clean machine
- [ ] ordering-app produces continuous synthetic traffic
- [ ] Promo-bearing checkouts cross the real HTTP boundary
- [ ] W3C trace context propagates — `promo.apply` and `promo.handle` share a trace ID
- [ ] Traces appear in Jaeger showing exactly two services
- [ ] The same trace IDs appear in PostgreSQL
- [ ] Parent-child relationships correct
- [ ] Trace heads contain correct span counts
- [ ] Transaction dimensions populate correctly
- [ ] Duplicate ingestion is idempotent and does not advance `last_span_ts`
- [ ] Ingest fence works
- [ ] Detector role cannot read `ground_truth`
- [ ] All 11 tests pass

Run the system and verify these yourself. Generating files and assuming they work is not Day 1 complete.

---

## Commands

```bash
cp .env.example .env
docker compose up --build -d
docker compose ps                          # wait for healthy
docker compose logs -f ordering-app        # confirm traffic

open http://localhost:16686                # Jaeger — find a promo trace

docker compose exec postgres psql -U postgres -d blastradius -c \
  "SELECT count(*) FROM span; SELECT count(*) FROM trace WHERE root_span_id IS NOT NULL;"

docker compose exec postgres psql -U blastradius_detector -d blastradius -c \
  "SELECT * FROM ground_truth;"            # must fail: permission denied

docker compose run --rm observability-service pytest -q
docker compose run --rm ordering-app pytest -q

docker compose down -v                     # reset everything
```

---

## Stop condition

When acceptance passes, **stop**. Do not begin Day 2.

Return a report with: files implemented · architecture actually running · tests executed and results · acceptance checklist · any deviations from v1.2 · known issues · exact local commands · recommended commit message.

If implementation reveals a genuine contradiction in the frozen contract, stop that portion of work, describe the issue, and propose the smallest corrective change. Do not silently redesign around it.

Suggested commit message:

```
feat: day 1 telemetry spine

Synthetic checkout emitting hierarchical OTel traces across ordering-app
and promo-provider, dual-exported to Jaeger and to the observability
service, persisted idempotently with trace-head denormalization.

- Alembic schema, three DB roles, detector isolation enforced by grants
- Truthful OTel resource identity, separate blastradius.domain attribute
- BlastRadiusSpanExporter with batching, retries, timeouts
- Idempotent ingest; duplicates do not advance the settle clock
- Ingest fence via ingest_state.last_reset_ts
- Seeded Poisson traffic generator at 150 orders/min
- 11 integration tests against real PostgreSQL

Refs: Technical Contract v1.2 §1-8, §24
```

**DAY 1 COMPLETE — AWAITING APPROVAL FOR DAY 2**
