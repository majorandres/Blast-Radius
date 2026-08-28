# Day 1 — Telemetry Spine — Design

**Date:** 2026-08-27
**Source of truth:** [Technical Contract v1.2](../../blast-radius-technical-contract-v1.2.md) (frozen) and
[Day 1 — Telemetry Spine](../../day-01-telemetry-spine.md).
Where this document and v1.2 disagree, v1.2 wins.

This document records the decisions the two source documents leave open, and the
five deviations Day 1 introduces. It does not redesign anything.

---

## 1. Scope

Day 1 builds a synthetic checkout that emits the §6.2 span tree across two real
processes, dual-exports it to Jaeger and to `observability-service`, and persists
it idempotently with trace-head denormalization, behind three Postgres roles whose
grants enforce detector isolation.

Out of scope: SLOs, incidents, attribution, blast radius, scenario controller,
frontend, LLM. `faults.py` exists as a scaffold with every behavior a no-op.

---

## 2. Runtime

Six Compose services:

| Service | Port | Notes |
|---|---|---|
| `postgres` | 5432 | 17-alpine |
| `migrate` | — | one-shot: Alembic, roles, grants, seed |
| `jaeger` | 16686, 4318 | all-in-one |
| `observability-service` | 8004 | ingest only on Day 1 |
| `promo-provider` | 8002 | real HTTP boundary |
| `ordering-app` | 8001 | checkout, traffic generator, bounded pool |

Python 3.12 in every image. 3.13 still has wheel-availability lag across the
OpenTelemetry contrib packages and nothing here requires it.

`frontend` is deferred entirely. A bare Vite scaffold buys nothing on Day 1 and
the contract does not gate on it until Day 3.

---

## 3. Module boundaries

```
packages/contracts/blastradius_contracts/
  telemetry.py     SpanEnvelope, SpanBatch
  attributes.py    every attribute key, domain name, and service name constant

apps/ordering_app/app/
  checkout.py                    orchestrates the span tree, owns no I/O
  dependencies/promo_client.py   promo.apply span + HTTP + CC-A timeout
  dependencies/payment.py        payment.authorize span
  dependencies/db.py             pool_acquire / persist_order, bounded pool
  telemetry/setup.py             TracerProvider, dual BatchSpanProcessor
  telemetry/exporter.py          BlastRadiusSpanExporter
  traffic/generator.py           Poisson arrivals, semaphore, seeded RNG
  faults.py                      scaffold, all no-ops

apps/promo_provider/app/
  main.py, config.py, faults.py
  telemetry/setup.py, telemetry/exporter.py

apps/observability_service/app/
  ingest/api.py    POST /internal/spans, request validation
  ingest/fence.py  last_reset_ts, cached in memory and reloaded on update
  ingest/writer.py the two SQL statements, nothing else
  db.py, config.py, main.py
```

Each `dependencies/*` module owns exactly one span and its domain constant, so
the CC-A rule (a CLIENT span's attribution domain is its peer, never its emitter)
is expressed in one place per dependency rather than scattered through
`checkout.py`. `checkout.py` calls them and knows nothing about spans beyond its
own root.

---

## 4. Span provenance — the four resolved decisions

The contract lists exactly eleven operations (§6.2). The OpenTelemetry
auto-instrumentors that gate zero mandates emit more than that. These four
decisions reconcile the two.

### 4.1 Manual spans, exporter filters

All eleven spans are created by hand with `blastradius.domain` set. The httpx and
FastAPI instrumentors stay **on**, purely so W3C trace context propagates and so
Jaeger shows a genuine auto-instrumented HTTP boundary.
`BlastRadiusSpanExporter` drops any span lacking `blastradius.domain`.

The detector therefore sees exactly the §6.2 tree. Jaeger sees the full truth.
This is consistent with §7.5: the observability service is not an OTLP receiver,
it consumes a custom JSON projection of spans emitted by a real SDK.

### 4.2 Parent id carried in a header

Filtering leaves `promo.handle` parented to an auto span that is never ingested.
`promo.apply` therefore injects its own span id as a `blastradius-parent` header
alongside `traceparent`. `promo-provider` reads that header and records it as a
`blastradius.parent_span_id` attribute on `promo.handle`; the exporter writes it
into `parent_span_id`, reconstructing the §6.2 edge.

### 4.3 In-process generator call, manual root span

The traffic generator's lifespan task calls the checkout coroutine directly and
opens `checkout` as a manual `SERVER`-kind root span. No loopback HTTP, no
self-call oddity under Compose, and the root span's transaction attributes are
set in one place.

`SERVER` kind on a function call is a mild fiction, accepted because the
alternative costs 150 loopback round trips per minute and puts a second httpx
client on the event loop competing with the promo client.

### 4.4 The exporter's projection rules

`BlastRadiusSpanExporter` applies exactly three rules, in order:

1. **Drop** any span without `blastradius.domain`.
2. **Override** `parent_span_id` from `blastradius.parent_span_id` when present.
3. **Map** `ReadableSpan → SpanEnvelope`; duration from the nanosecond delta;
   status `ERROR` iff `StatusCode.ERROR`.

Then POST a `SpanBatch`, retrying three times at 200/400/800 ms with an explicit
HTTP timeout on every call. On exhaustion, log and return
`SpanExportResult.FAILURE`. The exporter never raises into application code.

---

## 5. Data flow

```
generator ──in-process──► checkout()          manual SERVER root span
                            ├─ validate_order, pricing ─ loyalty_tier_lookup
                            ├─ db.pool_acquire          domain order-datastore
                            ├─ promo.apply ──HTTP──►  promo.handle  [promo-provider]
                            ├─ payment.authorize        domain payment-gateway
                            ├─ db.persist_order
                            ├─ analytics.publish        blocking=false
                            └─ confirmation

TracerProvider ├─ BatchSpanProcessor ─► OTLP ─────────► jaeger:4318
               └─ BatchSpanProcessor ─► BlastRadius ──► observability:8004/internal/spans
                                                            │
                                    fence: end_ts < last_reset_ts → drop, count
                                                            │
                                    writer: idempotent span insert + trace upsert,
                                            then root-population UPDATE
```

`loyalty_tier_lookup` is a child of `pricing`. `promo.handle` is a child of
`promo.apply` via §4.2. Everything else is a direct child of `checkout`.
`promo.apply` exists only when `has_promo` is true.

---

## 6. Persistence and the async stack

SQLAlchemy 2.x async with asyncpg.

SQLAlchemy is not optional: `pool_size=10, max_overflow=0, pool_timeout=5` and the
`sqlalchemy.exc.TimeoutError` that `db.pool_acquire` catches are SQLAlchemy
constructs, and §16.1 writes the code against them directly. The observability
service uses the same stack for consistency.

The §7.2 ingest CTE runs as a single `text()` statement with a generated
multi-row `VALUES` clause and bound parameters. No value is ever interpolated
into SQL as a string (§23).

`migrate` runs as `postgres`, so the three roles never own a table. It runs
Alembic, then creates the roles with `LOGIN` and env-supplied passwords, grants
`USAGE ON SCHEMA public` to each, applies the Day 1 grant block verbatim, and
seeds `service`, `domain`, `domain_edge`, and the single `ingest_state` row.

No `GRANT ... ON ALL TABLES` is issued anywhere. The `REVOKE` statements remain as
declared intent; the actual protection is that the grant was never made.

---

## 7. Error handling

| Path | Behavior |
|---|---|
| Malformed `SpanBatch` | `400`, logged, never `500` |
| Unknown service or domain name | `400` |
| Fenced span | dropped, counted, reported in `{accepted, fenced}` and on `/healthz` |
| Export failure | logged, `SpanExportResult.FAILURE`, checkout unaffected |
| Promo timeout at `PROMO_CLIENT_TIMEOUT_MS` | `promo.apply` ERROR, `error.kind=timeout`, domain `promo-provider` |
| Pool acquire timeout | `db.pool_acquire` ERROR, `error.kind=pool_timeout` |

The last two are structurally present but essentially unexercised on Day 1, since
no faults are enabled. Their tests are unit-level against an injected client stub
rather than end-to-end.

---

## 8. Testing

All eleven tests from the Day 1 doc. Tests 5–11 run against real PostgreSQL via
`docker compose run --rm <service> pytest`. No mocked database anywhere that
grant behavior or SQL semantics matter.

Test 11 — the detector role denied on `ground_truth` — is the isolation claim's
first real proof and is treated as a gate, not a checkbox.

---

## 9. Build sequence

1. Compose skeleton: postgres + jaeger only, both up.
2. Two bare FastAPI apps, OTel → Jaeger. **Gate zero — stop and verify** one
   trace id across both services in Jaeger before anything else is written.
3. Alembic schema, roles, grants, seed. **Gate one — stop and verify** test 11
   fails correctly with `InsufficientPrivilege`.
4. Full checkout span tree; verify shape in Jaeger.
5. `SpanEnvelope`, exporter, ingest endpoint, writer, trace head.
6. Fence.
7. Traffic generator.
8. Tests 1–11.

The rejected alternative was running 1–8 straight through and verifying at the
end. Gate zero exists because a propagation failure surfaces on Day 2 as what
looks like an attribution bug, and step 3's grant test is the thesis of the
project.

**Prerequisite:** the Docker daemon must be running. Nothing past step 1 is
verifiable without it.

---

## 10. Tooling decisions

- **Dependencies:** `uv` inside the images, with a `uv.lock` committed per app.
  Build context is the repo root so `packages/contracts` resolves as a path
  dependency. `uv` is not required on the host.
- **Version control:** git initialized at the repo root; the two source documents
  are the baseline commit.

---

## 11. Deviations from the Day 1 document

Carried into the Day 1 report's "deviations from v1.2" section as the stop
condition requires.

1. **Attribute constants live in `packages/contracts`**, not in
   `ordering_app/app/telemetry/attributes.py` "mirrored in the observability
   service." Three copies of strings that Day 2's detection logic depends on is a
   drift hazard, and the package already crosses all three processes.
2. **`blastradius.parent_span_id` attribute and `blastradius-parent` header** are
   new. The contract has no such field. They exist solely to reconstruct the §6.2
   parent edge across the filtered auto spans (§4.2).
3. **Exporter-side domain filtering** (§4.1) means "unknown `attribution_domain`
   → `400`" now fires only on genuinely malformed input rather than on every auto
   span.
4. **`checkout` is a manual `SERVER` root span** opened by an in-process generator
   call, not a FastAPI auto span (§4.3).
5. **Stale path reference:** the Day 1 document points at
   `docs/TECHNICAL-CONTRACT-v1.2.md`; the file is
   `docs/blast-radius-technical-contract-v1.2.md`.

---

## 12. Known open items for Day 2

Recorded here so they are not rediscovered later. Neither affects Day 1.

- §12.4 `attribute_latency` references an undefined `span_by_id` in its snippet;
  it iterates `spans`. A transcription slip in the contract, not a design flaw.
- The `slo` table's `min_samples` column and the profile's `slo_min_samples`
  setting overlap. Which one wins needs settling when the SLO engine is built.
