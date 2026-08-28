# Blast Radius

A synthetic e-commerce checkout that breaks in realistic ways, and an
observability service that works out **what** broke and **who it hit** — without
ever being told.

The second half of that sentence is the whole project. The detector holds no
database grant, no import, no API path, and no timing signal from the component
that injects faults. It reaches its conclusion from telemetry alone, it can be
wrong, and it says so when it is.

```text
click Inject  →  13s  first SLO breach
                 16s  incident opens, baseline frozen
                 22s  ATTRIBUTED: promo-provider, with the blast radius
                 28s  on screen, including poll latency
```

Then click **Reveal** to score it against ground truth the detector cannot read.

---

## Run it

```bash
cp .env.example .env
docker compose up --build -d
docker compose ps            # wait for healthy
```

| Surface | URL |
| --- | --- |
| **Dashboard** | <http://localhost:5173> |
| Jaeger | <http://localhost:16686> |
| Detector API | <http://localhost:8004/healthz> |

`ordering-app` starts generating traffic immediately — 150 orders/min, Poisson
arrivals, across three channels, two promotion states, and three payment methods.
Click **Inject blind fault** and watch.

A three-minute walkthrough is in [docs/DEMO.md](docs/DEMO.md).

---

## What it actually does

**Detects.** Two independent SLOs — checkout success and p95 latency. Independent
on purpose: under a fail-slow fault, latency degrades badly while availability
barely moves, and a system watching only success rate reports everything healthy.

**Attributes.** Walks each abnormal trace to a failure domain. Errors follow the
chain of blocking failures; slow traces go to whichever span owns at least 30% of
the wall time, and report no cause when nothing dominates.

**Measures blast radius.** Two different questions, never merged:

```text
                    IMPACT        CONCENTRATION
mobile              AFFECTED      PROPORTIONAL  0.86×
web                 AFFECTED      PROPORTIONAL  1.23×
aggregator          AFFECTED      PROPORTIONAL  1.07×
wallet              AFFECTED      CONCENTRATED  4.10×
card                UNAFFECTED    SPARED        0.00×
```

Everyone was hit; payment method explains it. Collapse those into one severity
column and the incident points you at "all channels", where there is nothing to
find.

**Explains.** An optional Claude-generated narrative, validated against the
evidence before display. **The app is fully functional with no API key** — a
deterministic renderer produces the same claims from the same evidence, and the
UI always labels which one you are reading.

---

## Three things worth knowing up front

**The detector is isolated by Postgres, not by convention.** Three roles. The
detector is denied on `ground_truth`, `scenario_run`, `reveal`, and `"order"`;
the scenario controller is denied on `span`, `trace`, and `incident`. Tested
against the real roles, in both directions, plus an AST import lint over the
detector's source.

**Every span carries two identities.** `emitting_service` is truthful OTel
resource identity — only two processes exist, and Jaeger shows exactly those two.
`attribution_domain` is the logical failure domain, and a CLIENT span's domain is
always its *peer*. That is why `payment-gateway` is a legal answer with no
process behind it, and why attribution survives a dependency timing out without
ever emitting a server span.

**DEMO compresses observation windows, not thresholds.** Detection logic,
thresholds, and attribution are identical in both profiles; only the windows
move. A test asserts it rather than asking you to trust it.

---

## Tests

```bash
docker compose run --rm --no-deps ordering-app pytest -q
docker compose run --rm --no-deps scenario-controller pytest -q
docker compose run --rm \
  -e DATABASE_URL_SCENARIO="postgresql+asyncpg://blastradius_scenario:scenario@postgres:5432/blastradius" \
  -e DATABASE_URL_APP="postgresql+asyncpg://blastradius_app:app@postgres:5432/blastradius" \
  observability-service pytest -q          # add -m "not slow" to skip live scenarios
```

Real PostgreSQL throughout — grants and SQL semantics are exactly what the tests
exist to check, so nothing mocks the database. The `slow` tests drive real
scenarios end to end against the running stack.

The negative suite is the interesting half: the detector denied on ground truth,
red herrings that fire hard and are never blamed, a healthy soak on both profiles
that opens zero incidents, and reveal refusing to score an incident from outside
the run window.

---

## Reset

```bash
curl -X POST http://localhost:8003/api/reset
```

Clears faults, drains both emitting processes, fences the ingest, and deletes
telemetry, incidents, orders, and scenario history — each service clearing only
what its own role owns. Developer affordance, not part of the demo.

---

## Documentation

| Document | Covers |
| --- | --- |
| [ARCHITECTURE](docs/ARCHITECTURE.md) | Components, the trust boundary, telemetry path, reset |
| [DETECTION](docs/DETECTION.md) | How a degraded checkout becomes a named domain |
| [DECISIONS](docs/DECISIONS.md) | Ten tradeoffs, and what each gave up |
| [FAILURE_MODES](docs/FAILURE_MODES.md) | What it cannot do and where it would mislead you |
| [DEMO](docs/DEMO.md) | Three-minute walkthrough |
| [Technical contract v1.2](docs/blast-radius-technical-contract-v1.2.md) | The frozen specification |

The most important limitation, stated first in FAILURE_MODES: **this has never
been shown to work on telemetry it did not generate.** The red herrings, the
healthy soak, and the honest misses narrow that gap. They do not close it.

---

## Layout

```text
apps/ordering_app            checkout, traffic generator, bounded pool, red herrings
apps/promo_provider          external dependency behind a real HTTP boundary
apps/observability_service   the detector: ingest, detection, blast radius, narrative
apps/scenario_controller     the injector: state machine, ground truth, reveal, reset
apps/frontend                Vite + React dashboard, hand-rolled SVG
packages/contracts           SpanEnvelope, attribute keys, OTel helpers, timing profiles
migrations                   Alembic schema, three roles, grants, seed
```

No auth, no TLS, no rate limiting, single node. This is a local proof of concept.
