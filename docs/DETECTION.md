# How detection works

From a degraded checkout to a named domain and a blast radius, with the
reasoning behind each step.

---

## The shape of the problem

A checkout touches four logical failure domains. When checkouts start failing or
slowing, the question is which one is responsible — answered only from spans,
with no knowledge that anything was injected.

```
checkout                 ordering-app      SERVER
  validate_order         ordering-app      INTERNAL
  pricing                ordering-app      INTERNAL
    loyalty_tier_lookup  ordering-app      INTERNAL     ← red herring 1
  db.pool_acquire        order-datastore   CLIENT
  promo.apply            promo-provider    CLIENT       ← peer domain (CC-A)
    promo.handle         promo-provider    SERVER
  payment.authorize      payment-gateway   CLIENT
  db.persist_order       order-datastore   CLIENT
  analytics.publish      ordering-app      INTERNAL     ← red herring 2, non-blocking
  confirmation           ordering-app      INTERNAL
```

The second column is the **emitting service** and only ever holds two values —
the two processes that exist. The third is the **attribution domain**, and for a
CLIENT span it is always the *peer*. That distinction is what lets attribution
name `payment-gateway`, which has no process, and survive a promo timeout where
no server span is emitted at all.

---

## 1. Two SLOs, deliberately independent

| SLO | Metric | Rule |
|---|---|---|
| `checkout_success` | confirmed / total over the window | ≥ 0.98 |
| `p95_latency` | p95 of `root_duration_ms` | ≤ 1000ms |

`error_rate` was removed as an exact mirror of `checkout_success`. Keeping p95
separate is what lets a **fail-slow** incident be seen at all: under pool
saturation latency degrades badly while availability barely moves, and a system
watching only success rate reports everything healthy throughout.

Both read `trace`, never `"order"` — the detector has no grant on `"order"`, and
under a datastore fault that row is missing precisely for the worst-affected
transactions.

---

## 2. Incident lifecycle

```
PENDING ──► OPEN ──► RECOVERING ──► CLOSED
```

Two consecutive breached evaluations open an incident. One bad window is noise;
at DEMO's 3s cadence, opening on a single window would raise an incident every
few minutes and no healthy soak would ever be clean.

A PENDING incident that recovers is **discarded**, not opened. Breaches during an
open incident append symptoms rather than creating new incidents — an incident is
a thing that happened, not a thing that was noticed repeatedly.

A window below `slo_min_samples` advances nothing in either direction. Thin data
is not healthy data: treating it as clean would close an incident during a lull.

**The baseline is frozen on `PENDING → OPEN` and never recomputed.** If it
followed the incident, the incident would slowly become its own baseline and
every verdict would drift toward UNAFFECTED however bad things got.

---

## 3. The abnormal population

```
abnormal_latency_threshold_ms = max(baseline_p95 × 3.0, 500)     frozen at open

abnormal ⟺ root_status = ERROR  OR  root_duration_ms > threshold
```

Attribution and concentration consume this **same** set. One definition of
abnormal exists in the system, which is what makes a zero-failure incident
characterisable.

The window starts at `first_breach_ts`, not at `opened_ts`: the traces that broke
the SLO are the incident's founding evidence.

---

## 4. Attribution — two paths

Chosen per trace by how that trace went wrong.

### Error path

Walk down from the root through **blocking** children whose status is ERROR,
taking the longest at each step. The domain where the walk stops is the answer.

Three properties, each load-bearing:

- only **blocking** children, so `analytics.publish` can never be blamed;
- only a chain **connected to the root**, so a handled error whose parent
  succeeded is not the culprit;
- the **domain** where it stops, so a client span whose peer never responded
  still attributes to the peer.

### Latency path

Compute self time for every blocking span — duration minus the **union** of child
intervals, clipped to the parent. The union matters: summing child durations
double-counts concurrency and yields negative self time exactly for the spans
that fanned work out.

The span owning the most wall time wins, but only if it owns at least **30%** of
the root's duration. Below that the trace was slow all over and reports no single
cause, which is more useful than naming whichever span edged ahead.

### Aggregation

```
share  = leader / candidates
gap    = share − runner_up_share

candidates < 5      → NO_DIAGNOSIS   (not enough evidence yet)
share < 0.40        → NO_DIAGNOSIS   (nothing explains enough of it)
gap < 0.15          → AMBIGUOUS      (two candidates too close to separate)
otherwise           → ATTRIBUTED
```

AMBIGUOUS is an honest answer and still scores as a miss. The exercise is to
identify the failing domain, not to narrow it to two.

---

## 5. Why the red herrings exist

Two permanent properties of the system, on in every scenario including the
healthy baseline. Neither is a fault and nothing injects them.

**`loyalty_tier_lookup`** goes from ~8ms to ~45ms under load — the largest
*relative* rise anywhere, about 5.6×, and roughly one percent of a multi-second
trace. Any detector ranking spans by how much they grew picks it every time.
Ranking by share of the root's wall time does not.

**`analytics.publish`** fails at up to 15% under load, independently of whether
the checkout succeeded, and is non-blocking. A detector taking "the deepest ERROR
span anywhere" blames `ordering-app` on traces that completed perfectly.

Measured live under fault: loyalty 8.1ms → 45.0ms, analytics 0% → 21% errors.
With both firing hard, attribution still lands 139/139 candidates on
`promo-provider`.

---

## 6. Blast radius — two different questions

### Impact: *did this cohort degrade, and how?*

Availability and latency judged **separately**, with the overall verdict derived
rather than measured:

```
availability   AFFECTED if inc_rate ≥ max(base + 0.10, base × 3.0)
               UNAFFECTED if inc_rate ≤ base + 0.02, else DEGRADED

latency        AFFECTED if inc_p95 ≥ max(base × 2.0, base + 500ms)
               UNAFFECTED if inc_p95 ≤ max(base × 1.2, base + 50ms), else DEGRADED

overall        the worse of the two known verdicts
```

Latency needs **both** a multiplicative and an absolute rise. Without the
absolute floor, a 20ms cohort tripling to 60ms reads AFFECTED and every fast
cohort cries wolf.

Judging them separately is what makes "slow, but not failing" a statement the
system can make.

### Concentration: *does this cohort explain where the abnormality is?*

```
ratio = (cohort_abnormal / total_abnormal) / (cohort_traces / total_traces)

≥ 2.0 → CONCENTRATED     ≤ 0.5 → SPARED     otherwise PROPORTIONAL
```

Equivalently: the cohort's abnormal rate divided by the system-wide abnormal
rate. 4× means this cohort goes wrong four times as often as traffic at large.

### They are not the same claim

Scenario B, measured live:

```
                    IMPACT        CONCENTRATION
mobile              AFFECTED      PROPORTIONAL  0.86×
web                 AFFECTED      PROPORTIONAL  1.23×
aggregator          AFFECTED      PROPORTIONAL  1.07×
wallet              AFFECTED      CONCENTRATED  4.10×
card                UNAFFECTED    SPARED        0.00×
other               UNAFFECTED    SPARED        0.00×

primary dimension: payment_method     primary cohort: wallet
```

Everyone was hit; payment method explains it. Collapsed into one severity column,
this incident points an operator at "all channels", where there is nothing to
find.

### The primary dimension

A dimension qualifies only when its top cohort is CONCENTRATED **and** at least
one sibling is SPARED. Without the spared sibling, a dimension gets called
discriminating merely because one of its values is busy — `channel=mobile` would
win nearly every incident on volume alone.

`primary_dimension = None` is a **positive finding**: it is how an infrastructure
fault, which hits everyone evenly, is told apart from a cohort-specific one. The
UI distinguishes that from "not computed yet", which is a different statement.

---

## 7. Timeline, measured

DEMO profile, from clicking Inject:

```
t+0    fault begins ramping (9s)
t+13   first SLO breach          → PENDING
t+16   second consecutive breach → OPEN, baseline frozen
t+22   ATTRIBUTED, with the primary dimension
t+28   visible in the browser, including poll latency
```

REALISTIC uses the same thresholds with a 300s window and 30s cadence. **No
detection threshold differs between profiles**, and a test asserts it.
