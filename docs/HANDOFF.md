# Handoff prompt

Copy everything below the line into a fresh Codex session started in the repo
root. Replace the **Your task** section with whatever you want it to work on.

---

You are picking up **Blast Radius**, a working project at
`c:\Users\junio\Desktop\projects-company\Blast Radius`. Read this whole brief
before touching anything.

## What it is

A synthetic e-commerce checkout that breaks in realistic ways, plus an
observability service that works out *what* broke and *who it hit* — without
being told. The detector holds no database grant, no import, no API path, and no
timing signal from the component that injects faults. That isolation is the
entire claim of the project; everything else exists to make it checkable.

The frozen specification is `docs/blast-radius-technical-contract-v1.2.md`. It is
the source of truth. Where the code deviates from it, the deviation is documented
at the definition site and collected in `docs/DECISIONS.md`.

## State: built and verified

All five planned days are complete and the release candidate was freshly
verified from an empty volume:

- **201 tests green** — 153 observability (including two live end-to-end scenario
  runs), 34 scenario-controller, 14 ordering-app.
- **Browser loop closes**: click Inject → 23s to a named domain → 31s to the
  blast radius → Reveal → CORRECT.
- All 19 non-video boxes of the contract's §26 MVP checklist are closed. The
  demo video remains a human task.

## Read these first, in this order

| File | Why |
|---|---|
| `README.md` | The claim, and how to run it |
| `docs/ARCHITECTURE.md` | Components, trust boundary, telemetry path, reset |
| `docs/DETECTION.md` | How a degraded checkout becomes a named domain |
| `docs/DECISIONS.md` | Ten tradeoffs + every contract deviation in one table |
| `docs/FAILURE_MODES.md` | What it cannot do; read before claiming it can |

## Running it

```bash
cp .env.example .env          # if .env is missing
docker compose up --build -d
docker compose ps             # wait for healthy — 7 containers
```

Dashboard at <http://localhost:5173>, Jaeger at <http://localhost:16686>,
detector at <http://localhost:8004/healthz>.

```bash
# fast suites
docker compose run --rm --no-deps ordering-app pytest -q
docker compose run --rm --no-deps scenario-controller pytest -q

# detector; drop -m "not slow" to include the live scenario runs (~5 min)
docker compose run --rm -e NARRATIVE_PROVIDER=stub \
  -e DATABASE_URL_SCENARIO="postgresql+asyncpg://blastradius_scenario:scenario@postgres:5432/blastradius" \
  -e DATABASE_URL_APP="postgresql+asyncpg://blastradius_app:app@postgres:5432/blastradius" \
  observability-service pytest -q -m "not slow"
```

**Rebuild the image after editing test or app code.** `docker compose run` uses
the built image, not your working tree — a stale image silently runs the old
file, which has already wasted time once.

## Invariants — do not break these

1. **The detector must never gain access to scenario state.** No grant, no
   import, no HTTP call to `scenario-controller`. Enforced by Postgres roles and
   an AST import lint (`tests/test_grants.py`). If a change needs the detector to
   know a scenario is running, the change is wrong.
2. **`emitting_service` stays truthful.** Only `ordering-app` and
   `promo-provider` exist as processes and Jaeger must show exactly those two.
   `attribution_domain` is separate and, for a CLIENT span, is always the *peer*.
3. **No detection threshold may differ between DEMO and REALISTIC.** DEMO
   compresses observation windows only. `tests/test_soak.py` asserts this.
4. **A checkout is unconditionally a trace root** (`root=True`). Inheriting
   ambient OTel context put 1386 spans on one trace once; don't reintroduce it.
5. **Impact and concentration are never merged** into one severity column. They
   answer different questions — see `docs/DETECTION.md` §6.
6. **No span older than `last_reset_ts` is ever ingested.** The fence is what
   makes the reset race deterministic instead of a sleep.

## Working agreements

- **Measure before tuning.** Every performance change in this repo was preceded
  by a timeline measurement from container logs. Guessing produced two wrong
  calibrations.
- **A test that passes suspiciously fast is a bug in the test.** The acceptance
  test once passed in 5.85s against a stale incident from a previous run. Live
  fixtures must assert the incident postdates the run.
- **Reset before any live measurement, then wait ~4 minutes.**
  `curl -X POST http://localhost:8003/api/reset`. The baseline window looks back
  four minutes; a poisoned baseline produces verdicts that are wrong in a
  flattering direction. This is the single most common way to get misleading
  results here.
- **Deviating from the contract is allowed; doing it silently is not.** Document
  it at the definition site and add it to the table in `docs/DECISIONS.md`.
- **Don't claim something works without running it.** Several bugs here were
  invisible in code review and obvious in a screenshot or a log timeline.

## Known-open work, roughly by value

1. **LLM eval fixtures** (contract §21.5). Ten fixtures captured from real
   scenario runs, frozen as JSON, including at least one uniform-impact /
   latency-only case to exercise narrative prompt rules 6 and 8. Currently only
   two hand-written fixtures exist in `tests/test_narrative.py`.
2. **CI is not yet independently verified.** A GitHub remote is configured, but
   the workflow's current Actions result has not been confirmed in this handoff.
3. **Scenario C — pool saturation** (contract §16, §26 stretch). Defined in
   `apps/scenario_controller/app/scenarios.py` but deliberately gated out of
   `IMPLEMENTED`. §16.4 warns it can flip attribution to `promo-provider` when
   pool waits exceed the 2s promo client timeout. If you enable it, the
   acceptance bar is: `ATTRIBUTED` to `order-datastore`, path `latency`, share
   ≥ 0.60, all cohorts availability UNAFFECTED with latency AFFECTED, and
   `primary_dimension is None`. Assert `promo-provider` share < 0.20. Cut it
   again if it misbehaves — a scenario that sometimes indicts the wrong domain
   would undermine the project's only claim.
4. **The §13.1 availability rule is unsatisfiable above a 1/3 baseline.**
   `AFFECTED` requires `inc_rate >= max(base + 0.10, base * 3.0)`, so a cohort
   failing 100% of the time reads DEGRADED once its baseline exceeds one third.
   Left as written because changing a frozen contract threshold is the owner's
   call. Documented in `docs/FAILURE_MODES.md` §3.
5. **Frontend has no unit tests.** Contract §21.6 asks for Vitest on the impact
   and concentration tables, including the "slow, not failing" case. Only the
   Playwright smoke test exists (`apps/frontend/tests/smoke.mjs`).
6. **Structured logging and the error envelope** (contract §19). Error codes are
   used but there is no `request_id` middleware or ULID correlation.

## Your task

<!-- Replace this with what you actually want done. -->

Start by running the fast test suites to confirm the stack is healthy on your
machine, then tell me what you find before changing anything.
