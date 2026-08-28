# Failure modes and limitations

What this system cannot do, where it would mislead you, and what would have to
change. Leading with the one that matters most.

---

## 1. It has never been shown to work on telemetry we did not generate

This is the honest headline.

Every trace it attributes was produced by code in this repository. The span tree
is one we designed, the domains are ones we assigned, and the faults are ones we
injected. A detector tuned against its own generator is not evidence that it
works against a system it has never seen.

Four things narrow the gap without closing it:

- **The red herrings.** Two permanent properties exist specifically to defeat
  naive attribution: a span with the largest relative latency rise in the system
  that is never the culprit, and a span that fails independently of the checkout
  outcome. Both fire hard during incidents and neither is ever blamed.
- **The healthy soak.** Twenty simulated minutes on both profiles, including
  isolated bad windows, opens zero incidents.
- **Honest misses.** The system reports AMBIGUOUS and NO_DIAGNOSIS, and scores
  both as failures. It is not built to always look right.
- **Blind mode.** The detector has no grant, import, or API path to the injector,
  enforced by Postgres and tested in both directions.

What would actually close it: running the detector against telemetry from a
service written by someone else, with faults chosen by someone else.

---

## 2. Concentration uses a ratio where a statistical test belongs

`concentration_ratio = abnormal_share / traffic_share`, thresholded at 2.0 and
0.5. The rigorous form is a chi-square test of independence between cohort
membership and abnormality, which would give a p-value instead of a hand-picked
cutoff.

The ratio is a deliberate simplification for demo-scale samples. Its practical
weakness: it does not know how confident it should be. A cohort with 12 traces
and a cohort with 1,200 traces both produce a ratio, and only `min_cohort_n`
stands between the small one and a spurious CONCENTRATED verdict.

---

## 3. The availability rule is unsatisfiable above a one-third baseline

`AFFECTED` requires `incident_rate >= max(baseline + 0.10, baseline × 3.0)`.

Once a baseline failure rate exceeds 1/3, that demands an incident rate above
1.0, which no failure rate can reach. **A cohort failing 100% of the time reads
DEGRADED, forever.**

This does not bite a healthy demo, where baselines sit near zero. It bit during
development when back-to-back injections poisoned a baseline window. It is a
latent flaw in the contract's own thresholds, left as written rather than
silently redesigned.

---

## 4. A poisoned baseline produces confidently wrong verdicts

The baseline is frozen over `[first_breach − 240s, first_breach − 20s]` (DEMO).
If a previous incident falls inside that window, the current one is measured
against a degraded reference and every verdict shifts toward UNAFFECTED — the
incident looks smaller than it is.

There is no detection for this. The acceptance tests work around it by waiting
for a genuinely healthy baseline before injecting, and `POST /api/reset` exists
partly to make that cheap. A production version would need to either exclude
known-incident windows from baselines or carry a confidence signal on the
baseline itself.

---

## 5. Attribution assumes one cause

The algorithm ranks domains and picks a leader. Two simultaneous independent
faults produce either AMBIGUOUS — honest, but unhelpful — or, if one dominates,
a confident verdict naming it while saying nothing about the other.

Real incidents are frequently two things at once, often causally linked. This
system has no vocabulary for that.

---

## 6. Thin cohorts silently drop out

A cohort below `min_cohort_n` reads INSUFFICIENT_DATA. At 150 orders/min the
`aggregator` channel is about 10% of traffic, so a 60s window holds roughly 15 of
them against a floor of 10 — it can legitimately fall out early in an incident.

The verdict is honest, but a reader scanning the table sees "TOO FEW" where they
expected a judgement, and the primary-dimension calculation skips any dimension
with an INSUFFICIENT_DATA member entirely. A genuinely discriminating dimension
can therefore go unreported because one of its values was quiet.

---

## 7. Detection latency is bounded below by the SLO window

An incident cannot be detected faster than the SLO window takes to move. Under
DEMO the window is 60s and diagnosis lands around 22s; under REALISTIC the window
is 300s and it would be several minutes.

That is correct behaviour — a shorter window would make p95 noisy and the healthy
soak dirty — but it means the demo's speed comes from compressed observation
windows, not from the detector being fast. The README says so and the profile
test asserts that no threshold differs between the two.

---

## 8. The narrative can still be unhelpful, just not wrong

Validation rejects a narrative that names an unaffected cohort as affected,
claims a cohort under uniform impact, describes a latency-only cohort as
failing, invents a slot, or writes a digit. It cannot reject one that is
accurate and useless.

The deterministic fallback has the opposite profile: never wrong, never
insightful. The UI always labels which one produced the text, because that
distinction matters more than the prose.

---

## 9. Orphan spans attach to the root, losing depth

When a span's parent was never ingested, it attaches to the trace root rather
than its true position. The error walk then sees it as a direct child of
`checkout`, which can shorten a causal chain.

In practice this affects the filtered auto-instrumentation spans, and the
propagated parent header handles the one case that matters. A trace with genuine
export loss in the middle would degrade quietly.

---

## 10. Single-node, single-tenant, no auth

No authentication, no TLS, no rate limiting, no multi-tenancy, one Postgres with
no replication. The reset endpoint deletes everything for everyone. `DELETE` is
used rather than `TRUNCATE` and is fast enough at ~200k spans; it would not be at
100×.

Stated in the README. This is a local proof of concept and nothing about its
operational shape should be read as production guidance.

---

## 11. The demo's realism has a ceiling

Traffic is Poisson at a fixed rate with independently drawn cohorts. Real traffic
has diurnal cycles, correlated cohorts, retries, hot keys, and clients that give
up. The payment gateway is `asyncio.sleep`. The promo timeout is the only real
network failure mode exercised.

The parts that are real — two processes, W3C context propagation across HTTP, a
genuinely bounded connection pool with measured wait times, real batch export —
are real. The rest is simulation, and the boundary is worth knowing before
drawing conclusions from a green dashboard.
