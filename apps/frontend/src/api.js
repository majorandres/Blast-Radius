// Two backends, deliberately not merged into one client. The detector and the
// injector are separate trust boundaries; keeping the calls apart here mirrors
// that instead of hiding it.

async function get(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${response.status} ${url}`);
  return response.json();
}

async function post(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(payload?.detail?.message || `${response.status} ${url}`);
    error.code = payload?.detail?.code;
    error.status = response.status;
    throw error;
  }
  return payload;
}

// --- detector (read only) ---
export const getHealth = () => get("/obs/api/health/current");
export const getTopology = () => get("/obs/api/topology");
export const getTimeseries = (minutes = 15) => get(`/obs/api/timeseries?minutes=${minutes}`);
export const getIncidents = (state) =>
  get(`/obs/api/incidents${state ? `?state=${state}` : ""}`);
export const getEvidence = (id) => get(`/obs/api/incidents/${id}/evidence`);

// --- injector ---
export const getCurrentRun = () => get("/ctl/api/scenarios/current");
export const getScore = () => get("/ctl/api/session/score");
export const inject = () => post("/ctl/api/scenarios/inject", { mode: "blind" });
export const stopRun = (id) => post(`/ctl/api/scenarios/${id}/stop`);
export const revealRun = (id, incidentId) =>
  post(`/ctl/api/scenarios/${id}/reveal`, { incident_id: incidentId ?? null });
