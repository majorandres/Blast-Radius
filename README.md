# Blast Radius

A telemetry-only incident investigator for a synthetic e-commerce checkout.

The checkout breaks in realistic ways. Blast Radius works out **what** broke
and **which customers were affected** without receiving the injected answer.
Reveal scores the diagnosis only after the detector has committed to it.

![Blast Radius diagnosing promo-provider, measuring affected cohorts, and receiving a CORRECT reveal score](docs/assets/blast-radius-revealed.png)

**Fresh empty-volume verification:** 7 runtime containers up, 6/6 configured
healthchecks passing, 201 tests green, a named failure domain in 23 seconds,
the blast radius in 31 seconds, and Reveal → **CORRECT**.

## Why I built it

An alert saying "checkout is failing" is only the beginning of an incident.
An engineer still needs to identify the responsible dependency, understand
whether every customer is affected or one cohort is concentrated among the
failures, and decide where to investigate first.

Blast Radius is a checkable experiment in automating that reasoning. The
detector is isolated from scenario state by database permissions, source-level
import checks, and the absence of any controller API client. Its conclusions
come from traces, can be wrong, and say so when the evidence is insufficient.

## At a glance

| Area | Implementation |
| --- | --- |
| Front end | React dashboard with topology, SLO charts, evidence, and reveal scoring |
| Back end | Four FastAPI services with async traffic and real HTTP boundaries |
| Data | PostgreSQL with three roles enforcing the detector/injector boundary |
| Observability | OpenTelemetry, W3C trace propagation, Jaeger, SLO detection, attribution |
| Reliability | Fault injection, reset fencing, healthy soaks, honest ambiguity and misses |
| Verification | 201 pytest tests, including live end-to-end scenarios, plus browser smoke |

That enforced isolation is the whole project. The detector holds no database
grant, no import, no API path, and no timing signal from the component that
injects faults. It reaches its conclusion from telemetry alone, it can be
wrong, and it says so when it is.

```text
click Inject  →  23s named domain  →  31s blast radius  →  Reveal
                                                            ↓
                                                         CORRECT
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

**Explains.** The default, no-key path renders a deterministic narrative from
the evidence. An optional Claude provider can replace the prose, but its output
is validated before display. **The app is fully functional with no API key**,
and the UI always labels which source produced the text.

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
