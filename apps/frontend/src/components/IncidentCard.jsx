import { useEffect, useState } from "react";
import { getEvidence } from "../api.js";

// Impact and concentration render side by side and are never merged.
//
// They answer different questions. Impact: did this cohort degrade, and how.
// Concentration: does this cohort explain where the abnormality is. A cohort
// can be badly hit and explain nothing -- under a wallet fault every channel is
// AFFECTED because wallets exist across all of them, while payment method is
// what actually discriminates. Collapsing these into one "severity" column
// would destroy the distinction the whole analysis exists to draw.

const IMPACT_CLASS = {
  AFFECTED: "v-affected",
  DEGRADED: "v-degraded",
  UNAFFECTED: "v-unaffected",
  INSUFFICIENT_DATA: "v-insufficient",
};

const CONCENTRATION_CLASS = {
  CONCENTRATED: "v-concentrated",
  PROPORTIONAL: "v-proportional",
  SPARED: "v-spared",
  INSUFFICIENT_DATA: "v-insufficient",
};

// Never colour alone: a glyph plus the word carries the meaning, and the colour
// only reinforces it.
const GLYPH = {
  AFFECTED: "▲",
  DEGRADED: "▲",
  UNAFFECTED: "●",
  INSUFFICIENT_DATA: "–",
  CONCENTRATED: "▲",
  PROPORTIONAL: "●",
  SPARED: "▼",
};

const LABEL = {
  INSUFFICIENT_DATA: "TOO FEW",
};

function Verdict({ value, kind = "impact" }) {
  const cls = (kind === "impact" ? IMPACT_CLASS : CONCENTRATION_CLASS)[value] ?? "v-insufficient";
  return (
    <span className={`verdict ${cls}`}>
      <span className="glyph" aria-hidden="true">{GLYPH[value] ?? "–"}</span>
      {LABEL[value] ?? value}
    </span>
  );
}

const DIMENSION_LABEL = {
  channel: "Channel",
  has_promo: "Promotion",
  payment_method: "Payment",
};

function cohortLabel(row) {
  if (row.dimension !== "has_promo") return row.value;
  return row.value === "true" ? "with promotion" : "no promotion";
}

// The one-line summary of §18. When no dimension discriminates, saying so is a
// positive finding -- it is how an infrastructure fault is told apart from a
// cohort-specific one -- not an absence of information.
function Summary({ incident, impact, concentration }) {
  const affected = impact.filter((r) => r.overall_verdict === "AFFECTED");
  const slowOnly = impact.filter(
    (r) => r.availability_verdict === "UNAFFECTED" && r.latency_verdict === "AFFECTED",
  );

  // `primary_dimension === null` means two different things and they must not be
  // conflated. Once concentration has been computed it is a positive finding --
  // no cohort explains this, so look at shared infrastructure. Before there are
  // enough abnormal traces to compute it, it means "not known yet", and saying
  // "spread evenly" there is a confident claim about data that does not exist.
  const rated = concentration.filter((c) => c.verdict !== "INSUFFICIENT_DATA");
  if (rated.length < 2) {
    return (
      <p className="summary">
        Still gathering evidence.
        <br />
        <span className="quiet">
          Too few abnormal traces so far to say which cohorts explain the abnormality.
        </span>
      </p>
    );
  }

  if (!incident.primary_dimension) {
    return (
      <p className="summary">
        {affected.length > 0
          ? "Every cohort degraded by a similar amount."
          : "Degradation is spread evenly."}
        <br />
        <span className="quiet">
          No business cohort explains the abnormality, which points at shared infrastructure
          rather than a particular kind of traffic.
        </span>
      </p>
    );
  }

  const primary = impact.find(
    (r) => r.dimension === incident.primary_dimension && r.value === incident.primary_cohort,
  );

  return (
    <p className="summary">
      {primary ? `Everyone ${cohortLabel(primary)} was affected.` : "A single cohort was affected."}
      <br />
      <span className="quiet">
        {DIMENSION_LABEL[incident.primary_dimension] ?? incident.primary_dimension} explains the
        concentration.
        {slowOnly.length > 0 &&
          ` ${slowOnly.length} cohort${slowOnly.length > 1 ? "s were" : " was"} slow but not failing.`}
      </span>
    </p>
  );
}

export default function IncidentCard({ incident }) {
  const [evidence, setEvidence] = useState(null);

  useEffect(() => {
    let live = true;
    if (!incident) { setEvidence(null); return; }
    getEvidence(incident.id).then((e) => live && setEvidence(e)).catch(() => {});
    return () => { live = false; };
  }, [incident?.id, incident?.candidate_trace_count]);

  if (!incident) {
    return (
      <div className="card">
        <h2>Incident</h2>
        <p className="empty">No active incident. The system is behaving.</p>
      </div>
    );
  }

  const impact = evidence?.impact ?? [];
  const concentration = evidence?.concentration ?? [];
  const byKey = Object.fromEntries(concentration.map((c) => [`${c.dimension}:${c.value}`, c]));

  return (
    <div className="card">
      <h2>Incident · {incident.state}</h2>

      <div className="verdict-headline">
        {incident.verdict === "ATTRIBUTED" ? (
          <>
            <span className="domain">{incident.attributed_domain}</span>
            <span className="meta">
              {Math.round((incident.attribution_share ?? 0) * 100)}% of{" "}
              {incident.candidate_trace_count} abnormal traces
            </span>
          </>
        ) : (
          <>
            <span className="domain">{incident.verdict ?? "ANALYSING"}</span>
            <span className="meta">
              {incident.verdict === "AMBIGUOUS"
                ? "two candidates too close to separate"
                : incident.verdict === "NO_DIAGNOSIS"
                  ? "no domain explains enough of the incident"
                  : "working"}
            </span>
          </>
        )}
      </div>

      {impact.length > 0 && (
        <Summary incident={incident} impact={impact} concentration={concentration} />
      )}

      {impact.length === 0 ? (
        <p className="empty">Gathering evidence…</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Cohort</th>
              <th>Availability</th>
              <th>Latency</th>
              <th>Overall</th>
              <th>Concentration</th>
              <th style={{ textAlign: "right" }}>p95</th>
            </tr>
          </thead>
          <tbody>
            {impact.map((row) => {
              const conc = byKey[`${row.dimension}:${row.value}`];
              return (
                <tr key={`${row.dimension}:${row.value}`}>
                  <td>
                    <span className="dim">{DIMENSION_LABEL[row.dimension] ?? row.dimension} · </span>
                    {cohortLabel(row)}
                  </td>
                  <td><Verdict value={row.availability_verdict} /></td>
                  <td><Verdict value={row.latency_verdict} /></td>
                  <td><Verdict value={row.overall_verdict} /></td>
                  <td>
                    {conc ? (
                      <>
                        <Verdict value={conc.verdict} kind="concentration" />
                        {conc.concentration_ratio != null && (
                          <span className="dim"> {conc.concentration_ratio.toFixed(1)}×</span>
                        )}
                      </>
                    ) : (
                      <span className="empty">—</span>
                    )}
                  </td>
                  <td style={{ textAlign: "right" }} className="dim">
                    {row.baseline_p95_ms != null && row.incident_p95_ms != null
                      ? `${Math.round(row.baseline_p95_ms)} → ${Math.round(row.incident_p95_ms)}ms`
                      : "—"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      {evidence && (
        <details className="drawer">
          <summary>Evidence</summary>
          <dl className="kv">
            <dt>Symptoms</dt>
            <dd>
              {evidence.symptoms.length
                ? evidence.symptoms
                    .map((s) => `${s.name} (${s.breach_count} breaches)`)
                    .join(", ")
                : "none recorded"}
            </dd>
            <dt>Culprit spans</dt>
            <dd>
              {Object.entries(evidence.attribution_detail?.culprit_operations ?? {})
                .map(([op, n]) => `${op} ×${n}`)
                .join(", ") || "—"}
            </dd>
            <dt>Span kinds</dt>
            <dd>
              {Object.entries(evidence.attribution_detail?.culprit_kinds ?? {})
                .map(([k, n]) => `${k} ×${n}`)
                .join(", ") || "—"}
            </dd>
            <dt>Unattributed</dt>
            <dd>{evidence.attribution_detail?.unattributed ?? 0} traces</dd>
            <dt>Abnormal threshold</dt>
            <dd>
              {evidence.baseline_snapshot?.abnormal_latency_threshold_ms != null
                ? `${Math.round(evidence.baseline_snapshot.abnormal_latency_threshold_ms)}ms (frozen at open)`
                : "—"}
            </dd>
            <dt>Baseline</dt>
            <dd>
              {evidence.baseline_snapshot?.p95_ms != null
                ? `p95 ${Math.round(evidence.baseline_snapshot.p95_ms)}ms over ${evidence.baseline_snapshot.n} traces`
                : "—"}
            </dd>
          </dl>
        </details>
      )}
    </div>
  );
}
