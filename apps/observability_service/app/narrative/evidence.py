"""Turn an Analysis into the only thing the narrator is allowed to see."""

from app.blast_radius.impact import CohortImpact
from app.narrative.contract import NarrativeEvidence


def cohort_label(row: CohortImpact) -> str:
    if row.dimension == "has_promo":
        return "with promotion" if row.value == "true" else "no promotion"
    return row.value


def build_evidence(analysis, domain_names: dict[int, str], domain_kinds: dict[str, str]):
    """Assemble NarrativeEvidence. Deliberately lossy.

    Everything the model could use to say something unsupported -- span detail,
    raw counts per domain, anything about a scenario -- is dropped here rather
    than filtered later.
    """
    a = analysis.attribution
    domain = domain_names.get(a.domain_id) if a.domain_id else None

    availability = [
        cohort_label(r) for r in analysis.impact if r.availability_verdict == "AFFECTED"
    ]
    latency = [
        cohort_label(r) for r in analysis.impact if r.latency_verdict == "AFFECTED"
    ]
    unaffected = [
        cohort_label(r) for r in analysis.impact if r.overall_verdict == "UNAFFECTED"
    ]

    return NarrativeEvidence(
        failure_domain=domain,
        failure_domain_kind=domain_kinds.get(domain or ""),
        verdict=a.verdict,
        attribution_count=a.counts.get(a.domain_id, 0) if a.domain_id else 0,
        candidate_count=a.candidate_count,
        attribution_share_pct=round(a.share * 100, 1),
        runner_up=domain_names.get(a.runner_up_id) if a.runner_up_id else None,
        runner_up_share_pct=round(a.runner_up_share * 100, 1) if a.runner_up_id else None,
        symptoms=[],
        availability_affected_cohorts=availability,
        latency_affected_cohorts=latency,
        unaffected_cohorts=unaffected,
        primary_dimension=analysis.primary_dimension,
        primary_cohort=(
            cohort_label(next(
                (r for r in analysis.impact
                 if r.dimension == analysis.primary_dimension
                 and r.value == analysis.primary_cohort),
                None,
            )) if analysis.primary_cohort else None
        ),
        uniform_impact=analysis.primary_dimension is None,
        dominant_path="error" if a.paths.get("error", 0) >= a.paths.get("latency", 0) else "latency",
    )
