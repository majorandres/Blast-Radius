"""Narrative providers, the fallback, and the runbooks (v1.2 §17).

Three providers, one interface. `ClaudeProvider` calls the API;
`StubProvider` returns a fixed valid narrative for tests; and the fallback is
not a provider at all but a deterministic renderer that always succeeds.

**The app is fully functional with no API key.** That is a requirement, not a
degraded mode: absence of a key is a supported state, and the UI labels which
source produced the text so nobody mistakes the template for the model.

Every prompt and response is appended to `logs/narrative.jsonl`, so a narrative
that turns out to be wrong can be traced back to exactly what was asked.
"""

import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from app.narrative.contract import SYSTEM_PROMPT, Narrative, NarrativeEvidence
from app.narrative.validate import ValidationFailure, render, validate

log = logging.getLogger(__name__)

MODEL = "claude-opus-5"
TIMEOUT_S = 8.0
NARRATIVE_LOG = Path(os.environ.get("NARRATIVE_LOG", "logs/narrative.jsonl"))

#: Keyed by domain *kind*, not by domain name: what you check for a datastore is
#: the same whether it is this datastore or another. The model never sees these
#: and never generates them (§17).
RUNBOOKS: dict[str, list[str]] = {
    "process": [
        "Check the service's own error logs and recent deploys.",
        "Compare its resource usage against the pre-incident baseline.",
    ],
    "logical_dependency": [
        "Check the provider's status page and any recent contract or quota change.",
        "Confirm client-side timeouts are shorter than the caller's own deadline.",
        "Consider whether the dependency should fail open for this path.",
    ],
    "datastore": [
        "Check connection pool saturation and wait times before query latency.",
        "Look for long-running transactions or a missing index on a hot path.",
        "Confirm the pool size matches current concurrency.",
    ],
}


def runbook_for(domain_kind: str | None) -> list[str]:
    return RUNBOOKS.get(domain_kind or "", [])


# --- the deterministic fallback -------------------------------------------
def fallback(evidence: NarrativeEvidence) -> dict[str, str]:
    """Always succeeds, always accurate, never interesting.

    Written from the same evidence and rendered through the same slot
    substitution, so it cannot drift from what the model would have been
    allowed to say.
    """
    if evidence.verdict != "ATTRIBUTED" or not evidence.failure_domain:
        what = (
            "The detector could not attribute this incident to a single domain."
            if evidence.verdict == "NO_DIAGNOSIS"
            else "The detector found two candidate domains it could not separate."
        )
        who = "Impact is recorded per cohort in the table above."
        check = "Review the per-domain evidence before acting."
        return {"what_happened": what, "who_was_affected": who, "what_to_check": check}

    narrative = Narrative(
        what_happened=(
            "The detector attributed this incident to {failure_domain}, which accounts "
            "for {attribution_share} of {candidate_count} abnormal traces."
        ),
        who_was_affected=(
            "Affected cohorts: {affected_cohorts}. Unaffected: {unaffected_cohorts}."
            if evidence.unaffected_cohorts
            else "Affected cohorts: {affected_cohorts}."
        ),
        what_to_check=(
            "Impact was uniform across cohorts, which points at shared infrastructure "
            "rather than a particular kind of traffic."
            if evidence.uniform_impact
            else "{primary_dimension} explains the concentration, centred on {primary_cohort}."
        ),
    )
    return render(narrative, evidence)


# --- providers -------------------------------------------------------------
class StubProvider:
    """Fixed, valid, and offline. Used on PRs so CI never spends money."""

    name = "stub"

    def generate(self, evidence: NarrativeEvidence) -> Narrative:
        return Narrative(
            what_happened=(
                "Checkout traffic degraded and the detector attributed it to "
                "{failure_domain}, covering {attribution_share} of the abnormal traces."
            ),
            who_was_affected="The affected cohorts were {affected_cohorts}.",
            what_to_check=(
                "Impact looks uniform, so start with shared infrastructure."
                if evidence.uniform_impact
                else "Start with {primary_dimension}, which explains the concentration."
            ),
        )


class ClaudeProvider:
    """Calls Claude with a small structured prompt and a hard timeout."""

    name = "claude"

    def __init__(self, api_key: str) -> None:
        import anthropic

        # Timeout is per §17. A narrative is a nicety; the incident card is
        # already complete without it, so waiting is worse than falling back.
        self._client = anthropic.Anthropic(api_key=api_key, timeout=TIMEOUT_S, max_retries=1)

    def generate(self, evidence: NarrativeEvidence) -> Narrative:
        payload = {
            "verdict": evidence.verdict,
            "failure_domain": evidence.failure_domain,
            "runner_up": evidence.runner_up,
            "symptoms": evidence.symptoms,
            "availability_affected_cohorts": evidence.availability_affected_cohorts,
            "latency_affected_cohorts": evidence.latency_affected_cohorts,
            "unaffected_cohorts": evidence.unaffected_cohorts,
            "primary_dimension": evidence.primary_dimension,
            "primary_cohort": evidence.primary_cohort,
            "uniform_impact": evidence.uniform_impact,
            "dominant_path": evidence.dominant_path,
        }
        response = self._client.messages.parse(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            # Short, highly constrained generation: low effort is the right
            # setting and keeps the call inside its 8s budget.
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": json.dumps(payload, indent=2)}],
            output_format=Narrative,
        )
        return response.parsed_output


def select_provider():
    """No key is a supported state, not a failure (§26)."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if os.environ.get("NARRATIVE_PROVIDER", "").lower() == "stub":
        return StubProvider()
    if not key:
        log.info("no ANTHROPIC_API_KEY; narratives will use the deterministic fallback")
        return None
    try:
        return ClaudeProvider(key)
    except Exception:
        log.exception("could not construct the Claude provider; using the fallback")
        return None


def _append_log(record: dict) -> None:
    try:
        NARRATIVE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with NARRATIVE_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError:
        log.warning("could not append to %s", NARRATIVE_LOG, exc_info=True)


def build(evidence: NarrativeEvidence, provider=None) -> tuple[dict[str, str], str]:
    """Returns (narrative fields, source).

    `source` is surfaced in the UI. A reader should never have to guess whether
    they are looking at generated prose or a template.
    """
    started = time.monotonic()
    failures: list[ValidationFailure] = []
    generated = None

    if provider is not None:
        try:
            generated = provider.generate(evidence)
            failures = validate(generated, evidence)
        except Exception as exc:  # noqa: BLE001 - any failure falls back
            log.warning("narrative generation failed: %s", exc)
            _append_log({
                "ts": datetime.now(UTC).isoformat(), "provider": provider.name,
                "error": str(exc), "outcome": "fallback",
            })
            generated = None

    if generated is not None and not failures:
        rendered = render(generated, evidence)
        _append_log({
            "ts": datetime.now(UTC).isoformat(), "provider": provider.name,
            "evidence": evidence.__dict__, "raw": generated.fields(),
            "rendered": rendered, "elapsed_s": round(time.monotonic() - started, 3),
            "outcome": "ok",
        })
        return rendered, provider.name

    if failures:
        log.warning("narrative rejected: %s", [f.check for f in failures])
        _append_log({
            "ts": datetime.now(UTC).isoformat(),
            "provider": getattr(provider, "name", None),
            "raw": generated.fields() if generated else None,
            "failures": [f.__dict__ for f in failures], "outcome": "rejected",
        })

    return fallback(evidence), "fallback"
