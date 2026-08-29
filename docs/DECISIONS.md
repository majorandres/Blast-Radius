# Decisions

Tradeoffs made building Blast Radius, with what was given up in each.

---

## 1. The observability service is not an OTLP receiver

**Decision.** Both emitting processes run two `BatchSpanProcessor`s on one
`TracerProvider`. One exports genuine OTLP to Jaeger. The other exports a custom
JSON projection — `SpanEnvelope` — to `observability-service`.

**Why.** Implementing an OTLP receiver would have consumed most of the build for
no gain in what the project is actually about. The spans are produced by a real
OpenTelemetry SDK with real context propagation across a real HTTP boundary;
what the detector consumes is a projection of those spans, not a re-invention of
them. Jaeger is in the stack precisely so the OTLP path is exercised for real
rather than claimed.

**Given up.** The detector cannot ingest telemetry from anything that does not
speak our envelope. A real observability backend must accept OTLP; this one
would have to grow that. Say this plainly rather than let a reader assume
otherwise.

---

## 2. Logical failure domains, separate from physical services

**Decision.** Every span carries two identities. `emitting_service` comes from
the OTel `Resource` and is truthful — only `ordering-app` and `promo-provider`
exist as processes, and Jaeger shows exactly those two. `attribution_domain` is
a span attribute naming the *logical* failure domain, and a CLIENT span's domain
is always its **peer**.

**Why.** Attribution has to answer "what broke", and what breaks is often not a
process you own. `payment-gateway` has no process behind it; `order-datastore`
is a connection pool. Both are legal attribution targets. More importantly, when
`promo-provider` stops responding, the client aborts at its timeout and **no
server span is ever emitted** — the only evidence is a CLIENT span in
`ordering-app`. Attributing by emitter would blame the caller for the callee's
failure, on exactly the incidents that matter most.

**Given up.** Two identities is a concept a reader has to hold. The temptation
to fake a `payment-gateway` service in Jaeger — which would make the topology
look richer — is refused, because it would be a lie about what is running.

---

## 3. Isolation enforced by Postgres grants, not by convention

**Decision.** Three database roles. The detector holds no grant on
`ground_truth`, `scenario_run`, `reveal`, or `"order"`. The scenario controller
holds none on `span`, `trace`, or `incident`. Verified in tests against the real
roles, in both directions, plus an AST-based import lint over the detector's
source.

**Why.** The entire claim of the project is that the detector reaches its
conclusion from telemetry alone. A comment saying so is worth nothing. A
permission denial is checkable, and it stays true when someone later adds a
convenient join.

**Given up.** Three connection strings, three role passwords, and a migration
that has to create roles *after* the schema (a `GRANT` inside a migration names
a role that does not exist yet). The reset sequence also became more intricate,
because each service can only delete what it owns.

---

## 4. Impact and concentration are separate questions

**Decision.** Each cohort gets an availability verdict, a latency verdict, a
derived overall verdict, **and** a separate concentration verdict computed over
the abnormal-trace population.

**Why.** They answer different questions and the difference is operationally
decisive. Under a wallet payment fault, every channel's failure rate rises —
mobile, web, and aggregator all read AFFECTED — because wallets exist across all
of them. No channel *explains* anything; payment method does. Collapsed into one
"severity" column, that incident points an operator at "all channels", where
there is nothing to find. Measured live: channels 0.86–1.23× (proportional),
wallet 4.10× (concentrated), card and other 0.00× (spared).

**Given up.** Two tables where one would be simpler, and a UI that has to teach
the distinction rather than assume it.

---

## 5. Concentration over abnormal traces, not failures

**Decision.** A trace is "abnormal" if it errored **or** exceeded a latency
threshold frozen at incident open. Attribution and concentration consume that
same population.

**Why.** Counting failures breaks on exactly the incident that most needs
characterising. Under pool saturation almost nothing fails — everything is
slow — so a failure-based measure returns INSUFFICIENT_DATA for every cohort and
reports nothing at all about a serious outage. One definition of abnormal,
shared by both analyses, covers fail-fast and fail-slow identically.

**Given up.** The threshold is a frozen multiple of baseline p95, which is a
heuristic. A trace one millisecond under it is "normal" and one millisecond over
is not.

---

## 6. The narrator writes placeholders, never digits

**Decision.** The model is forbidden digits and number words entirely. It writes
`{failure_domain}`, `{attribution_share}`, `{affected_cohorts}` and the renderer
substitutes real values afterwards, from the evidence.

**Why.** The dangerous failure is not a model that writes nonsense — that is
obvious. It is a model that writes a fluent, confident, *slightly wrong* number,
which reads exactly like a correct one. Forbidding digits converts the most
consequential hallucination from something that must be detected into something
that cannot be written.

**Given up.** Stiffer prose than a free-writing model would produce, and a slot
vocabulary that has to be maintained alongside the evidence model.

---

## 7. A verdict requires at least five abnormal traces

**Decision.** Below the floor, attribution reports NO_DIAGNOSIS, and the UI
distinguishes "not enough evidence yet" from "nothing explains this".

**Why.** Seconds after an incident opens the window holds a handful of traces,
and three of them agreeing produces "100% of 3" — a number that reads as
certainty and is an artifact of the sample size. This is a deviation from §12.5,
which sets no floor.

**Given up.** Roughly six seconds of detection latency. Deliberately set below
the profile's `min_abnormal_traces` (10), which gates concentration: ranking one
dimension needs less evidence than partitioning traffic into cohorts.

---

## 8. Red herrings read a smoothed load signal, not a semaphore

**Decision.** `analytics.publish` failure rate and `loyalty_tier_lookup` latency
both scale with an exponentially-weighted mean of in-flight checkouts.

**Why.** §14.3 specifies a shared semaphore for the loyalty herring. At 150
orders/min a semaphore guarding an ~8ms section inside a ~400ms checkout has
expected occupancy around 0.02 — it essentially never contends, and the observed
rise was coincidence rather than load. The first calibration also thresholded on
`in_flight / capacity`, where capacity is a safety cap of 40 and the system runs
at 1–6: **both herrings were effectively switched off, and the detector was
"defeating" obstacles that were not there.** That proves nothing at all.

**Given up.** Fidelity to the letter of §14.3. The semaphore is retained and
real; the load term is what makes the specified observable (8ms → 45ms under
load) happen reliably rather than by luck.

---

## 9. Every checkout is unconditionally a trace root

**Decision.** The `checkout` span starts with an empty OTel context.

**Why.** Found the hard way. `/internal/resume` starts the traffic generator
from inside a request handler, so `asyncio.create_task` captured that request's
active span and every subsequent checkout became its child — 1386 spans on a
single trace. It only manifested after a reset, and it would have silently
corrupted every post-reset session.

**Given up.** Nothing. A checkout is the start of its own trace by definition;
inheriting ambient context was never correct.

---

## 10. Diagnosis measured from first breach, not from incident open

**Decision.** The candidate window starts at `first_breach_ts`.

**Why.** §12.1 says `opened_ts`, which discards the traces that broke the SLO —
the incident's own founding evidence — and guarantees the first analysis pass
sees exactly zero candidates. A deviation, and the analysis is better for it.

**Given up.** Literal conformance to §12.1.

---

## Deviations from the frozen contract, collected

Each is documented at its definition site.

| Deviation | Where | Why |
|---|---|---|
| Attribute constants and OTel helpers in `packages/contracts` | `attributes.py`, `otel.py` | Three mirrored copies of strings Day 2 depends on is a drift hazard |
| `blastradius.parent_span_id` + header | `otel.py` | Five auto-instrumentation spans sit between `promo.apply` and `promo.handle` |
| Exporter drops spans lacking a domain | `exporter.py` | Keeps auto spans in Jaeger and out of the detector |
| Optional Claude narrative provider makes an outbound call | `provider.py` | §3's zero-outbound statement conflicts with §17's real-provider requirement; the default no-key path stays offline and the provider receives derived evidence only |
| `checkout` is a manual, unconditional root | `checkout.py` | See decision 9 |
| Incident window starts at first breach | `analysis.py` | See decision 10 |
| Attribution floor of five traces | `attribution.py` | See decision 7 |
| DEMO evaluates every 3s, not 5s | `profiles.py` | A cadence, not a threshold |
| DEMO ramp of 9s, not 15s | `profiles.py` | DEMO compresses windows 5× but left the ramp at 3× |
| Loyalty herring reads the load gauge | `herrings.py` | See decision 8 |
| Root-population `UPDATE` restricted to the batch | `writer.py` | Same result, bounded instead of scanning |

No deviation moves a detection **threshold**. DEMO and REALISTIC share every one,
and a test asserts it.
