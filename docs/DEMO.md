# Demo script

Roughly three minutes. The whole point is that nothing here tells the detector
what is wrong.

## Before you start

```bash
docker compose up --build -d
docker compose ps            # wait for healthy
```

Open `http://localhost:5173`. Then run a reset and **wait about four minutes**
before recording:

```bash
curl -X POST http://localhost:8003/api/reset
```

The reset clears telemetry, incidents, orders, and scenario history in one call.
The wait is not padding: the baseline window looks back four minutes, and an
incident measured against a degraded baseline produces verdicts that are wrong
in a specific direction. Details in [FAILURE_MODES](FAILURE_MODES.md#4-a-poisoned-baseline-produces-confidently-wrong-verdicts).

## The script

**1 — The system is healthy.** (~20s)

Orders flowing at 150/min, checkout success 100%, p95 around 215ms, all four
failure domains green. Note that `payment-gateway` and `order-datastore` are
domains with no process behind them — Jaeger at `localhost:16686` shows only the
two processes that actually exist.

**2 — Break something, without saying what.** (~10s)

Click **Inject blind fault**. The response deliberately withholds which scenario
it chose. Nothing about the fault reaches the detector: it holds no grant on the
scenario tables, imports nothing from the controller, and receives no call.

**3 — Watch it notice.** (~30s)

Checkout success falls, p95 crosses its SLO line, the system pill turns to
INCIDENT, and `promo-provider` goes red in the topology. First breach around 13
seconds, incident open around 16.

**4 — Watch it diagnose.** (~40s)

The incident card names **promo-provider** with the share of abnormal traces
behind it, then:

> Everyone with promotion was affected.
> Promotion explains the concentration.

Open the table. Every channel is AFFECTED but PROPORTIONAL — hit, and explaining
nothing. `with promotion` is CONCENTRATED at roughly 2.9×; `no promotion` is
SPARED at 0.0×. That distinction is the point of the whole analysis.

Open **Evidence** to show the culprit spans: `promo.apply` every time, always a
CLIENT span. The promo call aborts at its 2s timeout, so no server span exists at
all — attribution lands on the peer regardless.

**5 — Check the answer.** (~20s)

Click **Reveal**. CORRECT, with the injected fault named and the session score
updated.

## Worth mentioning while it runs

- **Two red herrings are firing the whole time.** `loyalty_tier_lookup` rises
  from 8ms to 45ms — the largest relative jump in the system — and
  `analytics.publish` fails around 20%. Neither is ever blamed.
- **DEMO compresses observation windows, not thresholds.** A test asserts that
  every detection threshold is identical to REALISTIC.
- **It can be wrong and says so.** Reveal renders INCORRECT and NO_INCIDENT, and
  both score as misses. AMBIGUOUS is a miss too.

## Scenario B, if there is time

```bash
curl -X POST http://localhost:8003/api/scenarios/inject \
  -H 'content-type: application/json' \
  -d '{"mode":"blind","scenario":"B"}'
```

Wallet payments fail. Every channel is AFFECTED and PROPORTIONAL; `wallet` is
CONCENTRATED at ~4×; `card` and `other` are SPARED. Same shape, different
explanation — and the attributed domain, `payment-gateway`, has no process behind
it at all.
