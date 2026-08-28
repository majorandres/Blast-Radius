import { useCallback, useEffect, useRef, useState } from "react";
import {
  getCurrentRun,
  getHealth,
  getIncidents,
  getScore,
  getTimeseries,
  getTopology,
  inject,
  revealRun,
  stopRun,
} from "./api.js";
import Charts from "./components/Charts.jsx";
import IncidentCard from "./components/IncidentCard.jsx";
import Topology from "./components/Topology.jsx";

// Polling cadences from §18. Health, topology, incidents, and scenario state at
// 2s; the timeseries at 5s because a 15s bucket cannot move faster than that.
function usePoll(fn, intervalMs, deps = []) {
  const [value, setValue] = useState(null);
  const saved = useRef(fn);
  saved.current = fn;

  useEffect(() => {
    let live = true;
    const tick = () =>
      saved.current().then((v) => live && setValue(v)).catch(() => {});
    tick();
    const id = setInterval(tick, intervalMs);
    return () => { live = false; clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, ...deps]);

  return value;
}

function HealthStrip({ health }) {
  const state = (health?.system_state ?? "HEALTHY").toLowerCase();
  return (
    <div className="card">
      <div className="strip">
        <div className="tile">
          <div className="label">Orders / min</div>
          <div className="value">{health?.orders_per_min ?? "—"}</div>
        </div>
        <div className="tile">
          <div className="label">Checkout success</div>
          <div className="value">
            {health?.checkout_success_pct != null ? health.checkout_success_pct.toFixed(1) : "—"}
            <span className="unit">%</span>
          </div>
        </div>
        <div className="tile">
          <div className="label">Latency p95</div>
          <div className="value">
            {health?.p95_latency_ms ?? "—"}
            <span className="unit">ms</span>
          </div>
        </div>
        <div className="tile">
          <div className="label">System</div>
          <div style={{ marginTop: 6 }}>
            <span className={`pill ${state}`}>
              <span className="dot" aria-hidden="true" />
              {health?.system_state ?? "—"}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

function ScenarioControls({ run, incident, score, onChanged }) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  const act = async (fn) => {
    setBusy(true);
    setError(null);
    try {
      return await fn();
    } catch (e) {
      // A 409 is not a scored result. It surfaces inline and the run stays open
      // for another attempt with a different incident.
      setError(e.code === "INCIDENT_OUTSIDE_RUN_WINDOW" ? e.message : e.message);
      return null;
    } finally {
      setBusy(false);
      onChanged();
    }
  };

  const live = run && !["COMPLETE", "REVEALED"].includes(run.state);

  return (
    <div className="card">
      <h2>Scenario</h2>
      <div className="controls">
        <button
          className="primary"
          disabled={busy || live}
          onClick={() => act(async () => { setResult(null); await inject(); })}
        >
          Inject blind fault
        </button>
        <button
          disabled={busy || !run || run.state === "REVEALED"}
          onClick={() => act(async () => setResult(await revealRun(run.id, incident?.id ?? null)))}
        >
          Reveal {incident ? "with this incident" : "(no incident found)"}
        </button>
        {live && (
          <button disabled={busy} onClick={() => act(() => stopRun(run.id))}>
            Stop
          </button>
        )}
        <span className="score">
          {run ? `Run ${run.state}` : "Idle"}
          {score ? ` · score ${score.correct}/${score.total}` : ""}
        </span>
      </div>

      {run?.mode === "blind" && live && (
        <p className="empty" style={{ marginTop: 10 }}>
          The fault is being injected. What broke is withheld until you reveal.
        </p>
      )}

      {error && <div className="inline-error">{error}</div>}

      {result && (
        <div className={`result ${result.correct ? "correct" : "incorrect"}`}>
          <h3>
            {result.correct
              ? "CORRECT"
              : result.detected_verdict === "NO_INCIDENT"
                ? "NO INCIDENT"
                : "INCORRECT"}
          </h3>
          <div className="detail">
            Detector said{" "}
            <strong>{result.detected_domain ?? result.detected_verdict}</strong>
            {result.detected_verdict !== "NO_INCIDENT" && result.detected_verdict !== "ATTRIBUTED"
              ? ` (${result.detected_verdict})`
              : ""}
            . Injected fault was in <strong>{result.injected_domain}</strong> (
            {result.injected_fault_type}). Session {result.session_correct}/{result.session_total}.
          </div>
        </div>
      )}
    </div>
  );
}

export default function App() {
  const [nonce, setNonce] = useState(0);
  const onChanged = useCallback(() => setNonce((n) => n + 1), []);

  const health = usePoll(getHealth, 2000);
  const topology = usePoll(getTopology, 2000);
  const series = usePoll(() => getTimeseries(15), 5000);
  const incidents = usePoll(() => getIncidents("active"), 2000);
  const run = usePoll(getCurrentRun, 2000, [nonce]);
  const score = usePoll(getScore, 2000, [nonce]);

  const incident = incidents?.[0] ?? null;

  return (
    <div className="app">
      <header className="masthead">
        <div>
          <h1>Blast Radius</h1>
          <div className="sub">
            Something breaks. The detector works out what — and who it hit — without being told.
          </div>
        </div>
        <div className="sub">
          DEMO profile · windows compressed, thresholds unchanged
        </div>
      </header>

      <HealthStrip health={health} />
      <ScenarioControls run={run} incident={incident} score={score} onChanged={onChanged} />

      <Topology topology={topology} />
      <Charts series={series} />
      <IncidentCard incident={incident} />
    </div>
  );
}
