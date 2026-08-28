# Blast Radius

A synthetic e-commerce checkout that breaks in realistic ways, and an
observability service that works out *what* broke and *who it hit* — without
ever being told.

The point of the project is the second half of that sentence. The detector holds
no grant, no API, no import, and no timing signal from the component that injects
faults. It reaches its conclusion from telemetry alone, and can be wrong, and
says so when it is.

**Status: Day 1 of 5 complete — the telemetry spine.** Nothing detects anything
yet. See [docs/day-01-telemetry-spine.md](docs/day-01-telemetry-spine.md) for
what that covers and
[docs/blast-radius-technical-contract-v1.2.md](docs/blast-radius-technical-contract-v1.2.md)
for the full contract.

## Running it

```bash
cp .env.example .env
docker compose up --build -d
docker compose ps                       # wait for healthy
```

| | |
|---|---|
| Jaeger | http://localhost:16686 |
| ordering-app | http://localhost:8001 |
| promo-provider | http://localhost:8002 |
| observability-service | http://localhost:8004/healthz |

`ordering-app` starts generating traffic immediately: 150 orders/min with
Poisson arrivals, across three channels, two promo states, and three payment
methods.

```bash
# telemetry landing in Postgres
docker compose exec postgres psql -U blastradius_detector -d blastradius \
  -c "SELECT count(*) FROM span; SELECT count(*) FROM trace WHERE root_span_id IS NOT NULL;"

# the isolation claim, enforced by the database
docker compose exec postgres psql -U blastradius_detector -d blastradius \
  -c "SELECT * FROM ground_truth;"      # must fail: permission denied

docker compose down -v                  # reset everything
```

## Tests

```bash
docker compose run --rm \
  -e DATABASE_URL_SCENARIO="postgresql+asyncpg://blastradius_scenario:scenario@postgres:5432/blastradius" \
  -e DATABASE_URL_APP="postgresql+asyncpg://blastradius_app:app@postgres:5432/blastradius" \
  observability-service pytest -q
```

Real PostgreSQL throughout. Nothing mocks the database, because grants and SQL
semantics are exactly what the tests exist to check.

## Two things worth knowing up front

**The observability service is not an OTLP receiver.** It consumes a custom JSON
projection of spans emitted by a real OpenTelemetry SDK. Jaeger receives genuine
OTLP on a second pipeline from the same `TracerProvider`. Both processes export
to both.

**Every span carries two identities.** `emitting_service` is truthful OTel
resource identity — only `ordering-app` and `promo-provider` exist as processes,
and Jaeger shows exactly those two. `attribution_domain` is the logical failure
domain, and a CLIENT span's domain is always its *peer*, never its emitter. That
is why `payment-gateway` and `order-datastore` are legal attribution targets
without any such process existing, and why attribution stays stable when a
dependency times out and never emits a server span at all.

## Layout

```
apps/ordering_app            checkout, traffic generator, bounded DB pool
apps/promo_provider          external dependency behind a real HTTP boundary
apps/observability_service   the detector: span ingest (Day 1), detection (Day 2)
packages/contracts           SpanEnvelope, attribute keys, shared OTel helpers
migrations                   Alembic schema, three roles, grants, seed
```

No auth, no TLS, no rate limiting. This is a local proof of concept.
