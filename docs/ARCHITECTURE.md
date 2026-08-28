# Architecture

Seven containers, one database, three roles, and one boundary that the whole
project exists to defend.

---

## The boundary

```
                        ┌──────────────────┐
                        │     frontend     │  :5173
                        │  run_id + inc_id │
                        └───┬──────────┬───┘
                   read     │          │  inject / reveal / reset
                            ▼          ▼
    ┌───────────────────────────┐   ┌──────────────────────────┐
    │  observability-service    │◄──│   scenario-controller    │
    │  :8004  THE DETECTOR      │   │   :8003  THE INJECTOR    │
    │                           │   │                          │
    │  ingest · SLO · incidents │   │  state machine           │
    │  attribution · impact     │   │  ground truth            │
    │  concentration · narrative│   │  fault dispatch          │
    │                           │   │  reveal validation       │
    │  NO OUTBOUND CALLS        │   │  reset orchestration     │
    └────────────┬──────────────┘   └───┬──────────────────┬───┘
    detector role│                      │ HTTP             │ HTTP
                 ▼                      ▼                  ▼
          ┌────────────┐        ┌──────────────┐   ┌──────────────┐
          │  postgres  │◄───────│ ordering-app │──►│promo-provider│
          │   :5432    │  app   │    :8001     │   │    :8002     │
          └────────────┘        └──────┬───────┘   └──────┬───────┘
                 ▲                     │  OTLP + export   │
                 └─── scenario ────────┴───── jaeger ─────┘
                                              :16686
```

The single arrow from controller to detector is a **read of the same public
endpoint the browser polls**. Observability cannot distinguish it from a
dashboard refresh, which is the only reason that direction is permitted at all.

`observability-service` makes **zero** outbound calls. That is what makes the
isolation claim checkable rather than asserted.

---

## Components

| Service | Port | Responsibility |
|---|---|---|
| `frontend` | 5173 | Dashboard. Holds both the run id and the incident id. |
| `ordering-app` | 8001 | Synthetic checkout, traffic generator, bounded DB pool. OTel resource `ordering-app`. |
| `promo-provider` | 8002 | External dependency behind a real HTTP boundary. OTel resource `promo-provider`. |
| `scenario-controller` | 8003 | Scenario lifecycle, ground truth, fault dispatch, reveal validation, reset. |
| `observability-service` | 8004 | Span ingest, SLO evaluation, attribution, blast radius, incidents, narrative, read API. |
| `postgres` | 5432 | One database, three roles. |
| `jaeger` | 16686 | Standards verification. Shows the two real processes. |

Drain and resume are endpoints on the emitting services, not new components.

---

## The three roles

Isolation runs both ways, enforced by Postgres and tested against the real roles.

| Role | Can read and write | Denied |
|---|---|---|
| `blastradius_detector` | `span`, `trace`, `incident`, `incident_symptom`, `ingest_state` | `ground_truth`, `scenario_run`, `reveal`, `"order"` |
| `blastradius_app` | `"order"` | `span`, `trace`, `incident`, all scenario tables |
| `blastradius_scenario` | `scenario_run`, `ground_truth`, `reveal` | `span`, `trace`, `incident`, `"order"` |

Tables are owned by `postgres`; no application role owns anything. The `REVOKE`
statements are declared intent — the actual protection is that the `GRANT` was
never made, and no `GRANT ... ON ALL TABLES` exists anywhere.

Roles are created **after** the migrations run, so grants live in
`migrations/bootstrap/roles.py` rather than inside a migration, where they would
name a role that does not exist yet.

---

## Telemetry path

```
checkout()  ──►  TracerProvider (Resource: service.name)
                  ├─ BatchSpanProcessor ──► OTLP ──────────► jaeger
                  └─ BatchSpanProcessor ──► BlastRadius ──► observability
                                                              │
                              fence: end_ts < last_reset_ts → dropped, counted
                                                              │
                              idempotent span insert + trace-head upsert
                                                              │
                                        root-population UPDATE
```

Both pipelines hang off **one** `TracerProvider` with one truthful resource.
Jaeger receives genuine OTLP; the detector receives a custom JSON projection —
see [DECISIONS](DECISIONS.md#1-the-observability-service-is-not-an-otlp-receiver).

### The exporter's three rules

1. **Drop** any span without `blastradius.domain`. That removes every
   auto-instrumentation span from the detector's view while Jaeger keeps them.
2. **Take the parent** from `blastradius.parent_span_id`, never from the OTel
   parent — which is frequently an auto span that was dropped.
3. **Map** `ReadableSpan → SpanEnvelope`.

Parentage is recorded explicitly via a contextvar rather than inferred, because
a parent can land in a different export batch. Across the promo hop it travels as
a header alongside `traceparent`: **five** auto-instrumentation spans sit between
`promo.apply` and `promo.handle`, and the header collapses them.

### Idempotent ingest

A fully duplicate batch inserts nothing, so `last_span_ts` does not advance and
the settle gate is not delayed. Re-delivery costs nothing and stalls nothing.

Transaction dimensions (channel, promo, payment method) are denormalized onto the
trace head at ingest, so blast radius never reads `"order"` — which the detector
cannot read, and which is missing precisely for the worst-affected transactions
when persistence fails.

### The fence

**Invariant: no span whose `end_ts` precedes `last_reset_ts` is ever ingested.**

Draining narrows the reset race; the fence closes it. A batch already crossing
the wire when the delete runs is older than the reset timestamp, so it is
rejected on arrival. That turns "no pre-reset span survives" from a timing
assumption into something a test can assert without sleeping.

---

## Reset

```
1-2  clear fault switches on both emitting services
3-4  drain: stop generating, await in-flight, force_flush
5    record reset_ts
6    detector: set the fence, then DELETE incident_symptom, incident, span, trace
7    app: DELETE "order", reseed the RNG (traffic stays stopped)
8    controller: DELETE reveal, ground_truth, scenario_run
9    resume baseline traffic
```

Faults are cleared and generation stopped **before** any deletion, so no
in-flight checkout lands a span in a freshly emptied table. Each step runs as the
role that owns the data, so the sequence needs no privilege that would weaken the
boundary. A drain timeout is a warning; any other failure stops the sequence and
leaves the system quiet rather than half-reset.

Measured: 201k spans, 21k traces, 28 incidents, 19k orders, 27 runs, 9 reveals —
all cleared in 0.46s.

---

## Repository

```
apps/ordering_app            checkout, traffic generator, bounded pool, herrings
apps/promo_provider          external dependency, real HTTP boundary
apps/observability_service   ingest · detection · blast_radius · narrative · api
apps/scenario_controller     state machine · dispatcher · reveal · reset
apps/frontend                Vite + React dashboard, hand-rolled SVG
packages/contracts           SpanEnvelope, attribute keys, OTel helpers, profiles
migrations                   Alembic schema, roles, grants, seed
```

`packages/contracts` holds everything that crosses a process boundary. The
attribute keys live there rather than being mirrored per service because Day 2's
detection logic depends on all three agreeing, and three copies drift.

`profiles.py` splits into `DetectionProfile` and `ScenarioProfile`. The detector
imports only the former, so no module it loads mentions scenario timing at all —
defence in depth behind the grants, not a substitute for them.
