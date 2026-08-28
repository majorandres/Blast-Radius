# Blast Radius — Technical Contract v1.2

**Status:** Final contract patch. Supersedes v1.1 in full.
**Scope of this revision:** FINAL-01 through FINAL-07 only. No redesign, no expansion, no new runtime components.
**Prior decisions:** PCC-01…PCC-10, RC1…RC9, CC-A/B/C all remain in force as written in v1.1.

---

# 1. Architecture Contract

## 1.1 Runtime components

| Component | Port | Responsibility |
|---|---|---|
| `frontend` | 5173 | Dashboard. Holds both `scenario_run_id` and `incident_id`. |
| `ordering-app` | 8001 | Synthetic checkout, traffic generator, payment simulation, bounded DB pool. OTel resource `ordering-app`. |
| `promo-provider` | 8002 | External dependency, real HTTP boundary. OTel resource `promo-provider`. |
| `scenario-controller` | 8003 | Scenario lifecycle, ground truth, fault dispatch, reveal validation, reset orchestration. |
| `observability-service` | 8004 | Span ingest, SLO evaluation, attribution, blast radius, incidents, narrative, frontend API. |
| `postgres` | 5432 | One database, three roles. |
| `jaeger` | 16686 | Standards verification. Process-level topology. |

No component added in v1.2. Drain and resume are endpoints on `ordering-app` and `promo-provider`, not new services.

## 1.2 Legal dependency directions

```
frontend ──────────────► observability-service     (read)
frontend ──────────────► scenario-controller       (control, reveal, reset)

scenario-controller ───► ordering-app              (faults, drain, reset, resume)
scenario-controller ───► promo-provider            (faults, drain)
scenario-controller ───► observability-service     (reveal read, reset)
scenario-controller ───► postgres [scenario role]

ordering-app ──────────► promo-provider            (instrumented HTTP)
ordering-app ──────────► postgres [app role]
ordering-app ──────────► observability-service     (span export)
ordering-app ──────────► jaeger                    (OTLP)
promo-provider ────────► observability-service     (span export)
promo-provider ────────► jaeger                    (OTLP)

observability-service ─► postgres [detector role]
```

`observability-service` makes zero outbound calls. This is what makes the isolation claim checkable.

## 1.3 Forbidden dependencies

The detector has no permission, API, import, or timing signal from the injector. `observability-service` must not read `ground_truth`, `scenario_run`, or `reveal`; must not learn whether a scenario is running, its id, state, start time, mode, seed, or identity; must not call or import `scenario-controller`; must not receive scenario information on any request body or header.

`scenario_run_public` does not exist. `incident.scenario_run_id` does not exist.

## 1.4 Diagram

```
                            ┌──────────────────┐
                            │     frontend     │
                            │  run_id + inc_id │
                            └───┬──────────┬───┘
                       read     │          │  inject / reveal / reset
                                ▼          ▼
        ┌───────────────────────────┐   ┌──────────────────────────┐
        │  observability-service    │◄──│   scenario-controller    │
        │  (detector)               │   │   (injector)             │
        │  ingest / SLO / attribution│  │  state machine           │
        │  impact / concentration   │   │  ground truth            │
        │  narrative                │   │  reveal validation       │
        │  NO outbound calls        │   │  reset orchestration     │
        └────────────┬──────────────┘   └───┬──────────────────┬───┘
        detector role│                      │ HTTP             │ HTTP
                     ▼                      ▼                  ▼
              ┌────────────┐        ┌──────────────┐   ┌──────────────┐
              │  postgres  │◄───────│ ordering-app │──►│promo-provider│
              └────────────┘ app    └──────┬───────┘   └──────┬───────┘
                     ▲                     │ OTLP + export    │
                     └─── scenario ────────┴──── jaeger ──────┘
```

## 1.5 Ground-truth isolation

Grants (§3.9), separate process, no timing signal, import lint. Tested in §21.4.

---

# 2. Repository Contract

Unchanged from v1.1, with these module additions:

```
apps/observability_service/app/
  ├── blast_radius/
  │   ├── impact.py            availability + latency + overall verdicts   (FINAL-01)
  │   └── concentration.py     abnormal-share concentration                (FINAL-02)
  ├── ingest/fence.py          last_reset_ts guard                         (FINAL-04)
  └── api/reset.py             POST /internal/reset

apps/ordering_app/app/
  └── drain.py                 stop generation, await in-flight, force_flush (FINAL-04)

apps/promo_provider/app/
  └── drain.py                 await in-flight, force_flush                 (FINAL-04)

apps/scenario_controller/app/
  ├── reveal.py                association validation                       (FINAL-05)
  └── reset.py                 drain-then-delete orchestration              (FINAL-04)
```

---

# 3. Database Contract

## 3.1 Enums

```sql
CREATE TYPE service_kind    AS ENUM ('process');
CREATE TYPE domain_kind     AS ENUM ('process','logical_dependency','datastore');
CREATE TYPE span_kind       AS ENUM ('INTERNAL','CLIENT','SERVER');
CREATE TYPE span_status     AS ENUM ('OK','ERROR');
CREATE TYPE checkout_status AS ENUM ('CONFIRMED','FAILED');
CREATE TYPE incident_state  AS ENUM ('PENDING','OPEN','RECOVERING','CLOSED');
CREATE TYPE scenario_state  AS ENUM ('IDLE','ARMED','INJECTING','ACTIVE','RECOVERING','COMPLETE','REVEALED');
CREATE TYPE attribution_verdict   AS ENUM ('ATTRIBUTED','AMBIGUOUS','NO_DIAGNOSIS');
CREATE TYPE impact_verdict        AS ENUM ('AFFECTED','DEGRADED','UNAFFECTED','INSUFFICIENT_DATA');
CREATE TYPE concentration_verdict AS ENUM ('CONCENTRATED','PROPORTIONAL','SPARED','INSUFFICIENT_DATA');
```

`impact_verdict` is now used three times per cohort: availability, latency, overall (FINAL-01).

## 3.2 Service and domain

```sql
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
```

Seed: `service` = `ordering-app`, `promo-provider`. `domain` = `ordering-app` (process), `promo-provider` (process), `payment-gateway` (logical_dependency), `order-datastore` (datastore). `domain_edge` = ordering-app → each of the other three.

`service` and `domain` share names where a process is also a domain. They are separate tables and must be joined by explicit id, never by name.

## 3.3 Span

```sql
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
```

`emitting_service_id` is truthful OTel resource identity and is never read by the detector. `attribution_domain_id` is the logical failure domain and is what attribution aggregates on.

## 3.4 Trace head

FINAL-06: the illustrative generated column is removed. All SQL in this contract executes as written.

```sql
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
```

Root columns are nullable because children routinely arrive before the root. A trace with `root_span_id IS NULL` is invisible to every query. Transaction dimensions are denormalized here from root-span attributes at ingest, so blast radius never reads `"order"` and continues working when persistence fails.

## 3.5 Application state

```sql
CREATE TABLE "order" (
  id uuid PRIMARY KEY,
  trace_id char(32) NOT NULL,
  channel text NOT NULL,
  has_promo boolean NOT NULL,
  payment_method text NOT NULL,
  status checkout_status NOT NULL,
  created_ts timestamptz NOT NULL
);
```

Owned by `blastradius_app`. Exists so `db.persist_order` does real work against a real bounded pool. No detector grant.

## 3.6 SLO

```sql
CREATE TABLE slo (
  id smallint PRIMARY KEY,
  name text NOT NULL UNIQUE,
  metric text NOT NULL,
  comparator text NOT NULL,
  threshold numeric NOT NULL,
  window_seconds integer NOT NULL,
  min_samples integer NOT NULL
);
```

Two rows: `checkout_success`, `p95_latency`. See §11.

## 3.7 Incident

```sql
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

CREATE TABLE incident_symptom (
  incident_id uuid NOT NULL REFERENCES incident(id) ON DELETE CASCADE,
  slo_id smallint NOT NULL REFERENCES slo(id),
  breached_ts timestamptz NOT NULL,
  observed_value numeric NOT NULL,
  PRIMARY KEY (incident_id, slo_id, breached_ts)
);
```

No scenario reference of any kind.

## 3.8 Scenario tables

```sql
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

CREATE TABLE reveal (
  scenario_run_id uuid PRIMARY KEY REFERENCES scenario_run(id),
  incident_id uuid,
  incident_first_breach_ts timestamptz,
  detected_domain text,
  detected_verdict text,
  correct boolean NOT NULL,
  revealed_ts timestamptz NOT NULL
);
```

`reveal.incident_id` is deliberately not a foreign key. A FK would require the scenario role to hold a reference on a detector-owned table and would create schema-level coupling contradicting §1.3. `incident_first_breach_ts` is recorded for audit of the FINAL-05 validation.

## 3.9 Grants (FINAL-03)

`DELETE` is used rather than `TRUNCATE` throughout. `TRUNCATE` requires a separate privilege and behaves differently under foreign keys; at demo scale (~50k rows) `DELETE` is fast enough and keeps the permission model simple.

```sql
-- detector: telemetry and incidents, including delete of its own state
GRANT SELECT, INSERT, UPDATE, DELETE ON span, trace, incident, incident_symptom
  TO blastradius_detector;
GRANT SELECT ON service, domain, domain_edge, slo TO blastradius_detector;
REVOKE ALL ON ground_truth, scenario_run, reveal, "order" FROM blastradius_detector;

-- app: its own order state only
GRANT SELECT, INSERT, DELETE ON "order" TO blastradius_app;
GRANT SELECT ON service, domain TO blastradius_app;
REVOKE ALL ON span, trace, incident, incident_symptom, ground_truth, scenario_run, reveal
  FROM blastradius_app;

-- scenario: its own scenario state only
GRANT SELECT, INSERT, UPDATE, DELETE ON scenario_run, ground_truth, reveal
  TO blastradius_scenario;
GRANT SELECT ON service, domain TO blastradius_scenario;
REVOKE ALL ON span, trace, incident, incident_symptom, "order" FROM blastradius_scenario;
```

Isolation runs both ways: the scenario role cannot read telemetry, and the detector role cannot read scenario state.

**Deletion order** (foreign keys):

| Role | Order |
|---|---|
| detector | `incident_symptom` → `incident`; `span` and `trace` in any order |
| app | `"order"` |
| scenario | `reveal` → `ground_truth` → `scenario_run` |

`incident_symptom` cascades on `incident` delete, but it is deleted explicitly first so the operation does not depend on cascade configuration.

---

# 4. Domain Model Contract

Changed models only. All others unchanged from v1.1.

```python
class CohortImpact(BaseModel):                       # FINAL-01
    dimension: Literal["channel","has_promo","payment_method"]
    value: str

    baseline_n: int
    incident_n: int

    baseline_failure_rate: float | None
    incident_failure_rate: float | None
    baseline_p95_ms: float | None
    incident_p95_ms: float | None

    availability_verdict: Literal["AFFECTED","DEGRADED","UNAFFECTED","INSUFFICIENT_DATA"]
    latency_verdict:      Literal["AFFECTED","DEGRADED","UNAFFECTED","INSUFFICIENT_DATA"]
    overall_verdict:      Literal["AFFECTED","DEGRADED","UNAFFECTED","INSUFFICIENT_DATA"]

class CohortConcentration(BaseModel):                # FINAL-02
    dimension: str
    value: str
    traffic_share: float
    abnormal_share: float                            # was failure_share
    abnormal_n: int                                  # abnormal traces in this cohort
    concentration_ratio: float | None
    verdict: Literal["CONCENTRATED","PROPORTIONAL","SPARED","INSUFFICIENT_DATA"]

class BlastRadius(BaseModel):
    impact: list[CohortImpact]
    concentration: list[CohortConcentration]
    primary_dimension: str | None
    primary_cohort: str | None
    total_abnormal: int                              # was total_failed
    total_traces: int
    abnormal_latency_threshold_ms: float             # frozen value used, for evidence
    baseline_window: tuple[datetime, datetime]
    incident_window: tuple[datetime, datetime]

class NarrativeEvidence(BaseModel):
    failure_domain: str | None
    verdict: str
    attribution_count: int
    candidate_count: int
    attribution_share_pct: float
    runner_up: str | None
    runner_up_share_pct: float | None
    symptoms: list[str]
    availability_affected_cohorts: list[str]         # FINAL-01
    latency_affected_cohorts: list[str]              # FINAL-01
    unaffected_cohorts: list[str]
    primary_dimension: str | None
    primary_cohort: str | None
    uniform_impact: bool
    dominant_path: Literal["error","latency"]

class RevealResult(BaseModel):
    scenario_run_id: UUID
    detected_domain: str | None
    detected_verdict: Literal["ATTRIBUTED","AMBIGUOUS","NO_DIAGNOSIS","NO_INCIDENT"]
    injected_domain: str
    injected_fault_type: str
    correct: bool
    session_correct: int
    session_total: int

class DrainResult(BaseModel):                        # FINAL-04
    generator_stopped: bool
    in_flight_remaining: int
    flush_succeeded: bool
    drained_at: datetime
```

---

# 5. API Contract

## 5.1 Frontend → observability-service

| Method | Path | Response |
|---|---|---|
| GET | `/api/health/current` | `{orders_per_min, checkout_success_pct, p95_latency_ms, system_state}` |
| GET | `/api/topology` | domain-level nodes and edges with per-domain health |
| GET | `/api/timeseries?metrics=…&minutes=15` | bucketed series |
| GET | `/api/incidents?state=active` | `list[Incident]` |
| GET | `/api/incidents/{id}` | `Incident` |
| GET | `/api/incidents/{id}/evidence` | attribution + impact + concentration + symptoms + narrative input |

## 5.2 Frontend → scenario-controller

| Method | Path | Request | Response |
|---|---|---|---|
| POST | `/api/scenarios/inject` | `{mode, scenario?, seed?, profile?}` | `ScenarioRun` |
| GET | `/api/scenarios/current` | — | `ScenarioRun \| null` |
| POST | `/api/scenarios/{id}/stop` | — | `ScenarioRun` |
| POST | `/api/scenarios/{id}/reveal` | `{incident_id: UUID \| null}` | `RevealResult` |
| GET | `/api/session/score` | — | `{correct, total}` |
| POST | `/api/reset` | — | `204` |

## 5.3 Internal

| Method | Path | Owner | Purpose |
|---|---|---|---|
| POST | `/internal/spans` | observability | Span batch ingest, `202`. Fence applied (§7.4). |
| POST | `/internal/reset` | observability | Delete telemetry and incident state |
| POST | `/internal/drain` | ordering-app | Stop generation, await in-flight, force-flush → `DrainResult` |
| POST | `/internal/drain` | promo-provider | Await in-flight, force-flush → `DrainResult` |
| POST | `/internal/reset` | ordering-app | Delete `"order"`, reseed RNG (traffic stays stopped) |
| POST | `/internal/resume` | ordering-app | Restart baseline traffic |
| PUT | `/_faults` | ordering-app | `{payment:{…}, db:{…}, traffic:{…}}` |
| PUT | `/_faults` | promo-provider | `{added_latency_ms, timeout_prob, failure_prob}` |
| GET | `/healthz`, `/readyz` | all | Compose readiness |

## 5.4 Reveal mechanism and validation (FINAL-05)

```
1. frontend POST /api/scenarios/inject                    → run_id
2. frontend polls observability /api/incidents            → incident_id (or none)
3. frontend POST /api/scenarios/{run_id}/reveal {incident_id}
4. controller GET observability:8004/api/incidents/{incident_id}
5. controller VALIDATES association (below)
6. controller compares detected vs ground_truth, writes `reveal`, returns RevealResult
```

**Validation rule.** Let `P` be the run's profile. The supplied incident is accepted only if:

```
run.started_ts  ≤  incident.first_breach_ts  ≤  run_end + P.recovery_hold_s + P.slo_window_s

where run_end = COALESCE(run.ended_ts, now())
```

The lower bound rejects an incident that began before the fault was armed. The upper bound allows for detection that lags the fault by at most one full SLO window plus the recovery hold, and rejects anything later.

On failure: `409` with code `INCIDENT_OUTSIDE_RUN_WINDOW`. Nothing is written to `reveal`; the run is not scored; the frontend surfaces the error and the user may retry with a different incident.

`incident_id = null` remains valid and means the detector opened no incident: `detected_verdict = "NO_INCIDENT"`, `correct = false`, scored as a miss.

This validation lives entirely in `scenario-controller`. Step 4 uses the same unauthenticated public read endpoint the frontend polls, so observability cannot distinguish it from a normal read and learns nothing.

---

# 6. Trace Contract

## 6.1 Two identities per span

| Concept | Source | Used by |
|---|---|---|
| `emitting_service` | OTel `Resource(service.name)`, per process | Jaeger, honesty |
| `attribution_domain` | span attribute `blastradius.domain` | detector |

Services: `ordering-app`, `promo-provider`. Domains: `ordering-app`, `promo-provider`, `payment-gateway`, `order-datastore`.

## 6.2 Span tree

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

## 6.3 CC-A — client spans carry the peer domain

When `promo-provider` times out, the client aborts at `PROMO_CLIENT_TIMEOUT_MS` and no server span is emitted. The deepest blocking ERROR span is then `promo.apply`, emitted by `ordering-app`. A CLIENT span's `attribution_domain` is therefore always its **peer**, never its emitter. Attribution is stable whether or not the peer responded.

## 6.4 Attributes

Root `checkout` only: `order.id`, `order.channel`, `order.has_promo`, `order.payment_method`.
Every span: `blastradius.domain`, `blastradius.blocking`.
Optional: `db.pool.wait_ms`, `http.status_code`, `payment.method`, `error.kind` ∈ `{timeout, upstream_error, pool_timeout}`.

## 6.5 Context propagation

W3C `traceparent` via `opentelemetry-instrumentation-httpx` and the FastAPI instrumentor. Verify in Jaeger on Day 1 before building anything else.

---

# 7. Telemetry Ingestion Contract

## 7.1 Pipeline

Dual `BatchSpanProcessor` on both emitting processes: one OTLP → Jaeger, one `BlastRadiusSpanExporter` → observability. `max_export_batch_size=200`, `schedule_delay_millis=2000`.

## 7.2 Idempotent ingest

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

A fully duplicate batch produces zero rows in `agg`, so `last_span_ts` does not advance and the settle gate is not delayed.

## 7.3 Settle gate

```
eligible ⟺ root_span_id IS NOT NULL
         ∧ now() - last_span_ts > profile.trace_settle_seconds
```

## 7.4 Ingest fence (FINAL-04)

`observability-service` holds `last_reset_ts`, persisted in a single-row `ingest_state` table and cached in memory.

```sql
CREATE TABLE ingest_state (
  id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  last_reset_ts timestamptz NOT NULL DEFAULT '-infinity'
);
GRANT SELECT, UPDATE ON ingest_state TO blastradius_detector;
```

**Invariant: no span whose `end_ts` precedes `last_reset_ts` is ever ingested.**

On `POST /internal/spans`, spans with `end_unix_nano < last_reset_ts` are dropped before insert. The response reports `{accepted, fenced}`. Fenced counts are logged and exposed on `/healthz` for the integration test.

Draining and flushing (§22) narrows the reset race; the fence closes it. An export batch already in flight over HTTP when the delete runs cannot survive, because its spans are older than the reset timestamp. This converts "no pre-reset span survives" from a timing assumption into an invariant, and makes the §21.2 test deterministic rather than sleep-based.

## 7.5 Dual export

`DECISIONS.md` must state: *the observability service is not an OTLP receiver; it consumes a custom JSON projection of OTel spans emitted by a real SDK.* Jaeger receives genuine OTLP.

---

# 8. Traffic Generator Contract

Poisson arrivals at 150/min, semaphore 40 in-flight, cohorts mobile/web/aggregator 55/35/10, `has_promo` 35%, payment card/wallet/other 60/25/15, seeded `random.Random`.

The semaphore is also the drain mechanism (§22): in-flight count is derived from it.

---

# 9. Scenario State Machine

`IDLE → ARMED → INJECTING → ACTIVE → RECOVERING → COMPLETE → REVEALED`. Ground truth is written in the `IDLE → ARMED` transaction, before any fault is dispatched. Ramp and hold durations come from the active profile. One non-terminal run at a time; a second inject returns `409`.

---

# 10. Incident State Machine

`PENDING → OPEN → RECOVERING → CLOSED`. Two consecutive breached evaluations open; three (REALISTIC) or two (DEMO) clean evaluations recover. All breaches during an open incident append `incident_symptom` rows rather than creating new incidents. `baseline_snapshot` is frozen on `PENDING → OPEN` and never recomputed.

---

# 11. SLO Contract

| id | name | metric | comp | threshold | rationale |
|---|---|---|---|---|---|
| 1 | checkout_success | `confirmed / total` over window | gte | 0.98 | Availability. Healthy ≈99.5%. |
| 2 | p95_latency | `percentile_cont(0.95)` of `root_duration_ms` | lte | 1000 | Independent of #1. Healthy ≈400ms. Catches fail-slow. |

`error_rate` was removed in v1.1 as an exact mirror of `checkout_success`. These two are genuinely independent: Scenario C breaches #2 while #1 holds.

Dependency timeout rates, pool-wait distributions, and per-domain error counts remain diagnostic evidence in the evidence drawer, not SLOs.

---

# 12. Attribution Algorithm Contract

## 12.1 Candidate (abnormal) trace selection

```
abnormal_latency_threshold_ms = max(baseline_snapshot.p95_ms * 3.0, 500)   # frozen at open

candidates = SELECT * FROM trace
  WHERE root_span_id IS NOT NULL
    AND root_end_ts BETWEEN incident.opened_ts AND now()
    AND now() - last_span_ts > profile.trace_settle_seconds
    AND (root_status = 'ERROR' OR root_duration_ms > abnormal_latency_threshold_ms)
```

This population is now shared: attribution consumes it, and concentration (§13.2) consumes the identical set. One definition of "abnormal" exists in the system.

## 12.2 Self time — interval union

Union of child intervals clipped to the parent, clamped at zero. Summing child durations yields negative self time whenever children overlap.

```python
def self_time_ms(span, children):
    intervals = sorted((c.start, c.end) for c in children if overlaps(c, span))
    merged, cur = [], None
    for s, e in intervals:
        s, e = max(s, span.start), min(e, span.end)
        if e <= s: continue
        if cur and s <= cur[1]: cur = (cur[0], max(cur[1], e))
        else:
            if cur: merged.append(cur)
            cur = (s, e)
    if cur: merged.append(cur)
    covered = sum((e - s) for s, e in merged)
    return max(0.0, span.duration_ms - covered)
```

## 12.3 Error path

```python
def attribute_error(root, children_by_id):
    node = root
    while True:
        kids = [c for c in children_by_id[node.span_id]
                if c.status == "ERROR" and c.blocking]
        if not kids:
            return node.attribution_domain_id, "error"
        node = max(kids, key=lambda c: c.duration_ms)
```

Descends only through blocking spans (excludes `analytics.publish`), only through a connected error chain from the root (a handled error whose parent succeeded is not a culprit), and returns a domain (so a client span whose peer never responded still attributes to the peer).

## 12.4 Latency path

```python
DOMINANCE = 0.30
def attribute_latency(root, spans, children_by_id):
    st = {s.span_id: self_time_ms(s, children_by_id[s.span_id])
          for s in spans if s.blocking}
    sid, best = max(st.items(), key=lambda kv: kv[1])
    if best / root.duration_ms < DOMINANCE:
        return None, "latency"
    return span_by_id[sid].attribution_domain_id, "latency"
```

## 12.5 Aggregation

```python
counts, unattributed = Counter(), 0
for t in candidates:
    fn = attribute_error if t.root_status == "ERROR" else attribute_latency
    dom, path = fn(...)
    if dom is None: unattributed += 1
    else: counts[dom] += 1

total  = len(candidates)
ranked = counts.most_common()
share  = ranked[0][1] / total if ranked else 0.0
runner = ranked[1][1] / total if len(ranked) > 1 else 0.0

if not ranked or share < 0.40:   verdict = "NO_DIAGNOSIS"
elif share - runner < 0.15:      verdict = "AMBIGUOUS"
else:                            verdict = "ATTRIBUTED"
```

## 12.6 Edge cases

| Case | Behavior |
|---|---|
| Zero candidates | `NO_DIAGNOSIS`, `total_candidates=0` |
| Root span never ingested | Trace invisible |
| Orphan span | Leaf of nearest present ancestor |
| Negative self time | Clamped to 0 |
| Root is culprit | Legal; `ordering-app` is a valid domain |
| Error with OK root | Not a candidate — handled failure |
| Promo timeout, no server span | Client span is culprit, domain `promo-provider` (CC-A) |
| Server span arrives after client timeout | Same domain either way |

---

# 13. Blast-Radius Contract

Two independent questions. Impact answers *did this cohort degrade, and how?* Concentration answers *does this cohort explain where the abnormality is?*

## 13.1 Impact — availability, latency, overall (FINAL-01)

v1.1 compared failure rates only, which contradicts Scenario C: latency degrades well before availability does, so every cohort would have read UNAFFECTED during the phase the scenario exists to demonstrate.

For each cohort value, over the frozen baseline window and the live incident window, compute failure rate and p95 of `root_duration_ms`.

```python
def availability_verdict(base_rate, base_n, inc_rate, inc_n, P):
    if base_n < P.min_cohort_n or inc_n < P.min_cohort_n:
        return "INSUFFICIENT_DATA"
    if inc_rate >= max(base_rate + 0.10, base_rate * 3.0):
        return "AFFECTED"
    if inc_rate <= base_rate + 0.02:
        return "UNAFFECTED"
    return "DEGRADED"

def latency_verdict(base_p95, base_n, inc_p95, inc_n, P):
    if base_n < P.min_cohort_n or inc_n < P.min_cohort_n:
        return "INSUFFICIENT_DATA"
    if inc_p95 >= max(base_p95 * 2.0, base_p95 + 500.0):
        return "AFFECTED"
    if inc_p95 <= max(base_p95 * 1.2, base_p95 + 50.0):
        return "UNAFFECTED"
    return "DEGRADED"
```

Latency thresholds require both a multiplicative and an absolute rise (the `max`), so a cohort with a small baseline is not flagged for a small absolute jump. At a 400ms baseline, AFFECTED requires ≥900ms and UNAFFECTED means ≤480ms.

**Overall verdict** is derived deterministically, never independently measured:

```python
SEVERITY = {"UNAFFECTED": 0, "DEGRADED": 1, "AFFECTED": 2}

def overall_verdict(availability, latency):
    known = [v for v in (availability, latency) if v != "INSUFFICIENT_DATA"]
    if not known:
        return "INSUFFICIENT_DATA"
    return max(known, key=lambda v: SEVERITY[v])
```

| availability | latency | overall |
|---|---|---|
| AFFECTED | any | AFFECTED |
| any | AFFECTED | AFFECTED |
| DEGRADED | UNAFFECTED / DEGRADED / INSUFFICIENT | DEGRADED |
| UNAFFECTED | DEGRADED | DEGRADED |
| UNAFFECTED | UNAFFECTED / INSUFFICIENT | UNAFFECTED |
| INSUFFICIENT | INSUFFICIENT | INSUFFICIENT_DATA |

Windows: baseline `[first_breach_ts − P.baseline_window_s, first_breach_ts − P.baseline_guard_s]`, frozen at incident open. Incident `[opened_ts, now]`. Both computed from `trace`, never from `"order"`.

## 13.2 Concentration — abnormal share (FINAL-02)

v1.1 used `failure_share / traffic_share` gated behind a minimum failure count. Scenario C produces almost no failures — only slow traces — so every cohort would have returned INSUFFICIENT_DATA on precisely the scenario concentration exists to characterize as uniform.

Concentration is therefore computed over the **abnormal trace population defined in §12.1**, using the same frozen `abnormal_latency_threshold_ms`. A trace is abnormal if `root_status = ERROR` **or** `root_duration_ms > abnormal_latency_threshold_ms`. This covers fail-fast and fail-slow identically.

```python
def concentration(a_v, n_v, A, N, P):
    """a_v: abnormal traces in cohort v   n_v: total traces in cohort v
       A:   total abnormal traces          N:   total traces (incident window)"""
    if n_v < P.min_cohort_n or A < P.min_abnormal_traces:
        return None, "INSUFFICIENT_DATA"
    traffic_share  = n_v / N
    abnormal_share = a_v / A
    ratio = abnormal_share / traffic_share      # == abnormal_rate(v) / abnormal_rate(overall)
    if ratio >= 2.0: return ratio, "CONCENTRATED"
    if ratio <= 0.5: return ratio, "SPARED"
    return ratio, "PROPORTIONAL"
```

The ratio is equivalently the cohort's abnormal rate divided by the system-wide abnormal rate, which is the more intuitive reading for the evidence drawer.

## 13.3 Primary discriminating dimension

```python
def primary(concentrations, P):
    best_dim, best_val, best_ratio = None, None, 0.0
    for dim, values in group_by_dimension(concentrations):
        if any(v.verdict == "INSUFFICIENT_DATA" for v in values):
            continue
        top = max(values, key=lambda v: v.concentration_ratio)
        siblings = [v for v in values if v is not top]
        if top.concentration_ratio >= 2.0 and any(s.verdict == "SPARED" for s in siblings):
            if top.concentration_ratio > best_ratio:
                best_dim, best_val, best_ratio = dim, top.value, top.concentration_ratio
    return best_dim, best_val
```

Requiring at least one SPARED sibling prevents a dimension being called discriminating merely because one value is busy. A dimension discriminates only when it separates the abnormal traces.

`primary_dimension = None` is a positive finding, not a failure: it distinguishes an infrastructure fault from a cohort-specific one.

## 13.4 Impact and concentration are not the same claim

Scenario B: wallet transactions fail; wallets exist across all channels; every channel's failure rate rises.

```
IMPACT (availability / latency / overall)      CONCENTRATION
mobile      AFFECTED / UNAFFECTED / AFFECTED   mobile      PROPORTIONAL (≈1.0×)
web         AFFECTED / UNAFFECTED / AFFECTED   web         PROPORTIONAL (≈1.0×)
aggregator  AFFECTED / UNAFFECTED / AFFECTED   aggregator  PROPORTIONAL (≈1.0×)
wallet      AFFECTED / DEGRADED   / AFFECTED   wallet      CONCENTRATED (≈4.0×)
card        UNAFFECTED/ UNAFFECTED/ UNAFFECTED card        SPARED       (≈0.0×)
other       UNAFFECTED/ UNAFFECTED/ UNAFFECTED other       SPARED       (≈0.0×)

PRIMARY DIMENSION: payment_method      PRIMARY COHORT: wallet
```

Everyone was hit; payment method explains it. A cohort may be impacted without explaining concentration, and that distinction is the whole point.

## 13.5 Documented simplification

A chi-square test of independence would be the rigorous form. The ratio is a deliberate simplification for demo-scale samples. Stated in `FAILURE_MODES.md`.

---

# 14. Scenario A — Third-Party Promo Degradation

**The only scenario required for MVP.**

## 14.1 Traffic
Flat at 150/min. No surge. A rise to 300/min is permitted only if the acceptance test also asserts `order-datastore` share < 0.15.

## 14.2 Fault
Ramp over `P.ramp_seconds`, hold `P.hold_seconds`:
```
promo-provider: added_latency_ms 0 → 3500, timeout_prob 0 → 0.30
promo client timeout 2000ms → most slow calls become client-side timeouts (CC-A path)
```

## 14.3 Red herrings — permanent system properties

`loyalty_tier_lookup`: 8ms → 45ms under load via a shared semaphore. Largest relative increase in the system (5.6×); ~1% of a 3.5s trace. Defeats multiplier-based detection.

`analytics.publish`: `blocking=false`, failure rate rises to 15% under load, independent of checkout outcome. Defeats "deepest ERROR span anywhere".

Both always on, in every scenario.

## 14.4 Expected results

| | Expected |
|---|---|
| Symptoms | checkout_success + p95_latency |
| Verdict | `ATTRIBUTED`, domain `promo-provider`, share ≥ 0.70, path predominantly `error` |
| Impact — `has_promo=true` | availability AFFECTED, latency AFFECTED, overall AFFECTED |
| Impact — `has_promo=false` | UNAFFECTED / UNAFFECTED / UNAFFECTED |
| Impact — channels | availability AFFECTED, latency AFFECTED or DEGRADED, overall AFFECTED |
| Impact — payment methods | availability AFFECTED or DEGRADED, overall AFFECTED or DEGRADED |
| Concentration | `has_promo=true` CONCENTRATED (≈2.9×); `has_promo=false` SPARED; channels and payment methods PROPORTIONAL |
| Primary | dimension `has_promo`, cohort `true` |
| Recovery | CLOSED within the profile recovery bound after fault clear |

Payment methods are not UNAFFECTED: promo orders spread across all payment methods, so all see abnormal traces.

## 14.5 Acceptance test

Deterministic under fixed seed, DEMO profile, CI:

```python
assert result.attributed_domain == "promo-provider"
assert result.attributed_domain != "ordering-app"                    # herring 2
assert "loyalty_tier_lookup" not in [t.culprit_operation for t in per_trace]   # herring 1
assert any(t.culprit_span_kind == "CLIENT" for t in per_trace)       # CC-A exercised
assert result.primary_dimension == "has_promo"
assert impact_of("has_promo", "true").availability_verdict == "AFFECTED"
assert impact_of("has_promo", "false").overall_verdict == "UNAFFECTED"
assert concentration_of("has_promo", "false").verdict == "SPARED"
```

---

# 15. Scenario B — Partial Payment Degradation

Flat 150/min. `payment.authorize` with `payment_method == "wallet"` gets `failure_prob = 0.55`, `added_latency_ms = 200`. Wallet is 25% of traffic → overall checkout success ≈86%.

Expected: `ATTRIBUTED` to `payment-gateway`, share ≥ 0.80, error path. Impact and concentration exactly as §13.4. Primary dimension `payment_method`, primary cohort `wallet`.

Test asserts availability verdicts exactly and latency verdicts loosely (`wallet` ∈ {DEGRADED, AFFECTED}; channels ∈ {UNAFFECTED, DEGRADED}), because channel-level p95 over a 25% affected sub-population is sensitive to distribution and not worth over-constraining.

---

# 16. Scenario C — Pool Saturation

## 16.1 Instrumentation
`pool_size=10, max_overflow=0, pool_timeout=5`. `db.pool_acquire` wraps the real `engine.connect()`; wait time is measured, not injected. Domain `order-datastore`, kind CLIENT.

```python
@asynccontextmanager
async def acquire():
    with tracer.start_as_current_span("db.pool_acquire",
            attributes={DOMAIN_KEY: "order-datastore", BLOCKING_KEY: True}) as span:
        t0 = time.perf_counter()
        try:
            conn = await engine.connect()
        except TimeoutError:
            span.set_status(ERROR); span.set_attribute("error.kind", "pool_timeout")
            raise
        span.set_attribute("db.pool.wait_ms", (time.perf_counter() - t0) * 1000)
    yield conn
```

## 16.2 Fault
`{db: {extra_concurrency: 25}}` spawns background tasks holding connections ~400ms each. No query latency added anywhere.

## 16.3 Expected

| | Expected |
|---|---|
| Symptoms | **p95_latency only** during the first phase; checkout_success may join later once pool timeouts begin |
| Verdict | `ATTRIBUTED` to `order-datastore`, path `latency`, share ≥ 0.60 |
| Impact — all cohorts | **availability UNAFFECTED, latency AFFECTED, overall AFFECTED** during the fail-slow phase |
| Concentration — all cohorts | PROPORTIONAL |
| Primary | `None` — uniform |

This is the case FINAL-01 and FINAL-02 exist for. Under v1.1 the impact table would have read UNAFFECTED everywhere (failure rates barely move) and concentration would have returned INSUFFICIENT_DATA everywhere (too few failures). Both now report correctly, and "latency degraded uniformly with no discriminating cohort" is exactly the right characterization of an infrastructure fault.

RC4 also matters here: `db.persist_order` fails, so `"order"` rows are never written for the worst-affected transactions. Blast radius reads `trace` and is unaffected. Tested explicitly.

## 16.4 Risks
Pool waits above 2s cause `promo.apply` to time out first, flipping attribution to `promo-provider`. Tune `extra_concurrency` for p95 pool wait of 800–1500ms; assert `promo-provider` share < 0.20. Cut by mid-Day 4 if not behaving. Stretch, not critical path.

---

# 17. AI Narrative Contract

Input is `NarrativeEvidence` only. No spans, no SQL, no ground truth, no scenario identity.

System prompt rules, with FINAL-01/02 additions:

```
1. Write NO digits and no number words. Use only these placeholders:
   {failure_domain} {attribution_count} {candidate_count} {attribution_share}
   {affected_cohorts} {unaffected_cohorts} {runner_up}
   {primary_dimension} {primary_cohort}
2. Name only domains present in the input.
3. Never describe an unaffected cohort as affected.
4. Do not speculate beyond the stated failure domain.
5. Two sentences maximum per field.
6. When uniform_impact is true, name no cohort as primarily responsible.
7. Distinguish cohorts that were affected from the cohort that explains the
   concentration. These are different claims. Do not merge them.
8. Distinguish availability impact from latency impact. If a cohort appears only
   in latency_affected_cohorts, do not describe it as failing.
```

Validation: schema check, no-digits regex, slot allowlist, failure domain named, no unaffected cohort described as affected, no PROPORTIONAL cohort named as primary, no primary cohort claim when `uniform_impact` is true, **no cohort described as failing when it appears only under latency impact**.

Timeout 8s. Any failure → deterministic `fallback.render(evidence)`, labeled in the UI via `narrative_source`. `StubProvider` in tests. Every prompt and response appended to `logs/narrative.jsonl`. Runbooks are a static dict keyed by attributed domain kind; the model never sees or generates them.

---

# 18. Frontend Data Contract

Polling: health and topology 2s, timeseries 5s, incidents 2s, scenario 2s.

The incident card renders **Impact** with three verdict columns (availability, latency, overall) and **Concentration** with the ratio, side by side, under a one-line summary:

```
Everyone using promotions was affected.
Promotion status explains the concentration.
```

or, when `primary_dimension is None`:

```
Latency degraded uniformly across channel, promotion status, and payment method.
No business cohort explains the abnormality.
```

Cohorts whose availability is UNAFFECTED but latency is AFFECTED render as "slow, not failing" so the two are never conflated visually.

Reveal renders CORRECT, INCORRECT, or NO_INCIDENT. A `409 INCIDENT_OUTSIDE_RUN_WINDOW` surfaces as an inline message, not a scored result.

---

# 19. Error Contract

```json
{"error":{"code":"...","message":"...","request_id":"01J..."}}
```

Codes: `VALIDATION_FAILED`, `NOT_FOUND`, `SCENARIO_ALREADY_ACTIVE`, `ILLEGAL_TRANSITION`, `INCIDENT_NOT_READY`, `INCIDENT_OUTSIDE_RUN_WINDOW` (FINAL-05), `NARRATIVE_UNAVAILABLE`, `UPSTREAM_TIMEOUT`, `UPSTREAM_UNAVAILABLE`, `RESET_IN_PROGRESS`, `DRAIN_TIMEOUT` (FINAL-04), `INTERNAL_ERROR`.

Every service maps unhandled exceptions to `INTERNAL_ERROR` with the traceback logged and never returned. `request_id` is a ULID from middleware, echoed in `X-Request-ID`.

---

# 20. Configuration Contract — Timing Profiles

| Setting | REALISTIC | DEMO |
|---|---|---|
| `slo_eval_interval_s` | 30 | 5 |
| `slo_window_s` | 300 | 60 |
| `slo_min_samples` | 50 | 40 |
| `breach_persistence` | 2 | 2 |
| `recovery_persistence` | 3 | 2 |
| `trace_settle_s` | 5 | 2 |
| `baseline_window_s` | 900 | 240 |
| `baseline_guard_s` | 60 | 20 |
| `min_cohort_n` | 30 | 10 |
| `min_abnormal_traces` | 20 | 10 |
| `scenario_ramp_s` | 45 | 15 |
| `scenario_hold_s` | 180 | 90 |
| `recovery_hold_s` | 60 | 25 |
| `drain_timeout_s` | 10 | 10 |
| `flush_timeout_s` | 5 | 5 |

`min_failures` is renamed `min_abnormal_traces` (FINAL-02) and now gates the abnormal population rather than the failure population.

CC-C: at 150 orders/min a 60s window holds ~150 traces, and the aggregator channel is ~15 of them. Cohort thresholds are therefore profile-scoped, and the baseline window stays substantially longer than the SLO window so baselines remain stable when the incident window is short.

Detection latency under DEMO: ~10–20s from injection to incident open. Recovery ≈ 70s.

`README.md` must state: *demo mode compresses observation windows so an incident lifecycle fits an interactive demonstration. Detection logic, thresholds, and attribution are identical in both profiles.*

Both profiles must pass the healthy soak. Other configuration unchanged from v1.1. `PROFILE=DEMO|REALISTIC` required, defaulting to `DEMO` in compose.

---

# 21. Testing Contract

## 21.1 Unit
Self time (concurrent, nested, clipped, negative); error walk with both herrings; client-span domain attribution with no server span; latency dominance boundary; aggregation verdicts at 0.39/0.40 and 0.14/0.15; **availability and latency verdict boundaries independently**; **overall verdict derivation table exhaustively, all 16 combinations**; **concentration ratio over the abnormal population, including a fixture with zero failures and many slow traces**; primary-dimension selection including the SPARED-sibling requirement; SLO persistence; incident dedup.

## 21.2 Integration
Order → export → ingest → trace head. Duplicate batch: `span_count` and `last_span_ts` unchanged, settle gate not delayed. Partial duplicate advances by new spans only. Children-before-root. Root arriving last populates all dimensions. Persistence failure: `"order"` absent, `trace` dimensions complete.

**Reset race (FINAL-04):**
1. Start traffic, let spans accumulate.
2. Hold a span batch in a test double so it is in flight.
3. Run the full reset sequence.
4. Release the held batch.
5. Assert every span in it is fenced, `span` and `trace` are empty, and the fenced counter incremented by exactly the batch size.

**Reset privileges (FINAL-03):** run the full reset with each service on its real role; assert no `InsufficientPrivilege` is raised and all owned tables are empty. Assert the detector role still fails on `ground_truth`.

## 21.3 Scenario
A, B, C against §14.4 / §15 / §16.3, DEMO profile, fixed seed. Scenario C additionally asserts `availability_verdict == "UNAFFECTED"` and `latency_verdict == "AFFECTED"` for at least one cohort during the fail-slow phase, and `primary_dimension is None`.

## 21.4 Negative — the tests that prove the detector isn't cheating
1. `loyalty_tier_lookup` never the culprit operation in any scenario.
2. `analytics.publish` failures never produce an `ordering-app` attribution.
3. `SELECT * FROM ground_truth` on the detector connection raises `InsufficientPrivilege`; same for `scenario_run` and `reveal`.
4. Import lint: no `observability_service/` file references `scenario_controller`, `ground_truth`, `scenario_run`, or `reveal`.
5. Healthy soak, both profiles: 20 simulated minutes at baseline, zero incidents opened.
6. Ambiguity fixture (45/42) → `AMBIGUOUS`, both candidates returned.
7. Wrong-answer honesty: incorrect attribution fixture → `correct=false`, UI renders INCORRECT.
8. No-incident honesty: sub-threshold fault → `incident_id=null` → `NO_INCIDENT`, `correct=false`.
9. Schema isolation: `incident` has no column referencing any scenario table.
10. **Reveal association (FINAL-05):** an incident whose `first_breach_ts` precedes `run.started_ts` → `409 INCIDENT_OUTSIDE_RUN_WINDOW`, nothing written to `reveal`, session score unchanged. Same for an incident beyond the upper bound.

## 21.5 LLM evals
10 fixtures captured from real scenario runs, frozen as JSON. Must include at least one Scenario C fixture (`uniform_impact=true`, latency-only impact) to exercise prompt rules 6 and 8. Real provider on `main`, `StubProvider` on PRs.

## 21.6 Frontend
Vitest on impact and concentration table rendering, including the "slow, not failing" case. Playwright smoke: load → inject → incident → reveal.

---

# 22. Reset Contract

Each process clears only what its role owns. `DELETE` throughout (FINAL-03). Drain precedes deletion (FINAL-04), and the ingest fence (§7.4) closes the residual race.

`POST /api/reset` on `scenario-controller`:

```
 1. → ordering-app     PUT  /_faults {}          clear all fault switches
 2. → promo-provider   PUT  /_faults {}          clear all fault switches
 3. → ordering-app     POST /internal/drain      stop generation;
                                                 await in-flight ≤ drain_timeout_s;
                                                 force_flush(flush_timeout_s);
                                                 → DrainResult
 4. → promo-provider   POST /internal/drain      await in-flight; force_flush
                                                 → DrainResult
 5.   controller       record reset_ts = now()
 6. → observability    POST /internal/reset      set last_reset_ts = reset_ts;
                                                 DELETE incident_symptom, incident,
                                                        span, trace
 7. → ordering-app     POST /internal/reset      DELETE "order"; reseed RNG
                                                 (traffic remains stopped)
 8.   controller       DELETE reveal, ground_truth, scenario_run   (own role)
 9. → ordering-app     POST /internal/resume     restart baseline traffic
10.   return 204
```

Faults are cleared and generation stopped **before** any deletion, so no in-flight checkout produces a span that lands in a freshly emptied table. Step 5 records the fence timestamp before step 6 deletes, so any batch already in flight is rejected on arrival rather than inserted.

If step 3 or 4 reports `in_flight_remaining > 0` at timeout, the reset proceeds — the fence makes late spans harmless — but the response includes a `DRAIN_TIMEOUT` warning for visibility. Any other step failing returns `500 RESET_IN_PROGRESS` and leaves the system stopped rather than half-reset.

Developer affordance. Not exposed in the demo UI.

---

# 23. Security / Trust Boundary Contract

| Concern | Treatment |
|---|---|
| LLM key | Env only, never logged, absence is a supported state |
| Scenario input | Pydantic enums; no free-form strings reach the dispatcher |
| SQL injection | SQLAlchemy parameter binding throughout; cohort dimension names from a hardcoded allowlist, never request input |
| Malformed telemetry | `SpanBatch` capped at 500; unknown domain or service rejected `400`; malformed batch dropped with a log, never a 500 |
| Ground-truth leakage | §1.3, §3.9, tested in §21.4 |
| Prompt injection | Prompt contains only enum values and integers from our own database. No external text path exists. Documented as not applicable, with a note on what would change with real telemetry |
| Dependency timeouts | Every outbound HTTP call has an explicit timeout |
| Resource exhaustion | Traffic semaphore; batch cap; timeseries capped at 60 minutes |
| Reveal integrity | FINAL-05 association validation prevents scoring against an unrelated incident |

No auth, no TLS, no rate limiting. Local POC, stated in the README.

---

# 24. Docker Compose Contract

| Service | Ports | Depends on (condition) |
|---|---|---|
| `postgres` | 5432 | — |
| `migrate` | — | postgres (healthy) |
| `jaeger` | 16686, 4318 | — |
| `observability-service` | 8004 | migrate (completed) |
| `promo-provider` | 8002 | — |
| `scenario-controller` | 8003 | migrate (completed) |
| `ordering-app` | 8001 | observability, promo, postgres (healthy) |
| `frontend` | 5173 | observability (healthy) |

Healthchecks: `pg_isready`; `GET /readyz` at 5s intervals, 12 retries. `migrate` is one-shot: Alembic, role creation and grants, seed of `service`, `domain`, `domain_edge`, two `slo` rows, and the single `ingest_state` row. `PROFILE=DEMO` on observability and controller.

---

# 25. Day-by-Day Implementation Order

**Day 1 — Telemetry spine.** Repo, compose, migrations with roles and grants, checkout with the §6.2 span tree, promo-provider, traffic generator, OTel on both processes with truthful resources, dual export, idempotent ingest, ingest fence, trace head with dimensions.
*Acceptance:* complete trace heads with correct `span_count`; same trace in Jaeger showing a two-process topology; duplicate batch changes nothing; **`traceparent` verified propagating across the promo call before anything else is built**; detector role denied on `ground_truth`.

**Day 2 — Detection.** SLO engine, incident lifecycle with dedup and frozen baseline, candidate selection, self time, both attribution paths, aggregation, impact (three verdicts), concentration (abnormal share), primary dimension, scenario-controller with state machine and ground truth, Scenario A, both herrings.
*Acceptance:* §14.5 passes. Detector has no scenario knowledge. Herring and CC-A tests pass.

**Day 3 — Product.** Dashboard, health strip, domain topology graph, charts, timeline, incident card with both tables, evidence drawer, blind inject, reveal with validation and `NO_INCIDENT`, score, animation.
*Acceptance:* full loop in the browser under DEMO profile, injection to diagnosis under 30 seconds.
**Hard gate: record the demo video at end of Day 3.**

**Day 4 — Depth.** Scenario B, Scenario C (cut mid-day if §16.4 risks appear), narrative provider with validation and fallback, runbooks, eval fixtures, full negative suite, healthy soak both profiles, reset orchestration with drain and fence.

**Day 5 — Credibility.** CI, structured logging, error contract, readiness, README, ARCHITECTURE, DECISIONS, DETECTION, FAILURE_MODES, DEMO, diagram, screenshots, final video, deploy last and only if stable.

---

# 26. Definition of Done

## MVP
- [ ] Traces span two real processes with truthful OTel resource names
- [ ] `attribution_domain` distinct from `emitting_service` throughout
- [ ] Client spans attribute to their peer, tested with no server span
- [ ] Duplicate ingest changes nothing and does not delay the settle gate
- [ ] No span older than `last_reset_ts` is ever ingested
- [ ] Trace head carries transaction dimensions; blast radius never reads `"order"`
- [ ] Detector has no grant, API, import, or timing signal from the injector
- [ ] `incident` has no scenario reference
- [ ] Two SLOs, no mirrored pair
- [ ] **Impact reports availability, latency, and a derived overall verdict per cohort**
- [ ] **Concentration computed over the abnormal population, working with zero failures**
- [ ] Primary dimension computed, `None` on uniform impact
- [ ] **Scenario A only** end-to-end; both herrings present and not attributed
- [ ] Blind inject → diagnose → reveal → CORRECT / INCORRECT / NO_INCIDENT
- [ ] Reveal rejects an incident outside the run window
- [ ] DEMO profile: injection to diagnosis under 30s
- [ ] Healthy soak clean under both profiles
- [ ] Reset drains, fences, and violates no grant
- [ ] `docker compose up` on a clean machine
- [ ] **Demo video exists**

## Portfolio-ready
- [ ] Scenario B passing; concentration identifies `wallet`
- [ ] LLM narrative with slot filling, validation, labeled fallback; app works with no API key
- [ ] Narrative never describes a latency-only cohort as failing
- [ ] Ambiguity, no-incident, and out-of-window paths tested
- [ ] CI green across all six test categories
- [ ] README understandable in 30 seconds; profile compression stated plainly
- [ ] DECISIONS.md ≥ 5 tradeoffs including the custom-exporter admission and the logical-vs-physical topology split
- [ ] FAILURE_MODES.md ≥ 7 limitations including the chi-square simplification

## Stretch
Scenario C · LLM evals in CI · SSE · deployment · payment as a separate process

---

# 27. Final Consistency Audit (FINAL-07)

| Check | Result |
|---|---|
| Scenario C can show latency impact while availability is healthy | **Yes.** `latency_verdict` is computed independently of failure rate; §16.3 expects availability UNAFFECTED with latency AFFECTED; the overall verdict derives to AFFECTED; §21.3 asserts it. |
| Concentration works identically for error and latency incidents | **Yes.** Both use the §12.1 abnormal population (`ERROR` **or** over the frozen latency threshold). A zero-failure, many-slow-trace fixture is a required unit test. |
| No reset operation violates the DB grants | **Yes.** Each step runs under the owning role, `DELETE` is granted on exactly those tables, deletion order respects foreign keys, and §21.2 runs the sequence on real roles. |
| No buffered pre-reset trace survives into a post-reset session | **Yes.** Drain plus force-flush narrows the window; the `last_reset_ts` fence closes it as an invariant. §21.2 tests the held-batch race deterministically. |
| Reveal scoring cannot use an unrelated incident | **Yes.** Server-side window validation in the controller; `409` and no write on failure; §21.4.10 tests both bounds. |
| Detector isolation unchanged | **Yes.** No new grant, endpoint, table, or field reaches the detector. The controller's reveal read uses the existing public endpoint. `ingest_state` is detector-owned and carries no scenario information. |
| Scenario A remains the only required MVP scenario | **Yes.** §26 MVP lists Scenario A alone; B is portfolio-ready; C is stretch. |
| No new runtime component or infrastructure dependency | **Yes.** Drain and resume are endpoints on existing services. `ingest_state` is a table, not a component. No new image, port, or dependency. |

## Remaining known weakness

Attribution still cannot be shown to work on telemetry whose generating process we did not write. The herrings, the healthy soak, honest misses, and documented failure modes narrow the gap without closing it. `FAILURE_MODES.md` leads with this rather than waiting for the question.

---

# CHANGES FROM v1.1

| Change | Sections |
|---|---|
| **FINAL-01** `CohortImpact` reports availability, latency, and a deterministically derived overall verdict; explicit thresholds for each; baseline and incident p95 recorded per cohort as evidence | 3.1, 4, 13.1, 14.4, 15, 16.3, 17, 18, 21.1, 21.3, 26 |
| **FINAL-02** Concentration computed over the §12.1 abnormal-trace population rather than failures; `abnormal_share / traffic_share`; `min_failures` renamed `min_abnormal_traces`; works for fail-fast and fail-slow alike | 4, 12.1, 13.2, 14.4, 15, 16.3, 20, 21.1, 26 |
| **FINAL-03** `DELETE` replaces `TRUNCATE`; per-role `DELETE` grants on owned tables only; all cross-boundary revocations preserved; deletion order documented for foreign keys | 3.9, 22, 21.2 |
| **FINAL-04** Drain sequence added (stop generation, await in-flight, force-flush, confirm) on both emitting processes; ingest fence via `last_reset_ts` makes "no pre-reset span survives" an invariant; deterministic held-batch race test | 2, 4, 5.3, 7.4, 22, 21.2, 24 |
| **FINAL-05** Server-side reveal association validation in the controller with an explicit window rule; `409 INCIDENT_OUTSIDE_RUN_WINDOW`; `incident_first_breach_ts` recorded for audit; `incident_id=null` remains the valid NO_INCIDENT path | 3.8, 5.4, 19, 21.4, 26 |
| **FINAL-06** Illustrative generated column removed from `trace`; all SQL in this contract executes as written | 3.4 |
| **FINAL-07** Full consistency audit performed; no contradiction found | 27 |

No other changes. All v1.1 decisions (PCC-01…10, RC1…9, CC-A/B/C) remain in force.

---

`READY FOR IMPLEMENTATION PENDING APPROVAL`
