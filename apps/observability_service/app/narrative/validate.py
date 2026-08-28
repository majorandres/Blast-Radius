"""Validate a generated narrative before anyone sees it (v1.2 §17).

Every check here exists because the corresponding failure would be *plausible
and wrong* -- the dangerous kind. A narrative that names an unaffected cohort as
failing, or credits a proportional cohort with explaining the incident, reads
exactly as fluently as a correct one. Fluency is not the signal, so it cannot be
the check.

A failed check is not an error to surface. It falls back to the deterministic
renderer and the UI labels which one it is showing.
"""

from dataclasses import dataclass

from app.narrative.contract import (
    ALLOWED_SLOTS,
    DIGIT_PATTERN,
    NUMBER_WORDS,
    SLOT_PATTERN,
    Narrative,
    NarrativeEvidence,
)


@dataclass(frozen=True)
class ValidationFailure:
    check: str
    detail: str


def _words(text: str) -> set[str]:
    return {w.strip(".,;:!?()'\"").lower() for w in text.split()}


def validate(narrative: Narrative, evidence: NarrativeEvidence) -> list[ValidationFailure]:
    failures: list[ValidationFailure] = []
    fields = narrative.fields()
    everything = " ".join(fields.values())

    # --- 1. no digits, no number words -----------------------------------
    for name, text in fields.items():
        if DIGIT_PATTERN.search(text):
            failures.append(ValidationFailure("no_digits", f"{name} contains a digit"))
        overlap = _words(text) & NUMBER_WORDS
        if overlap:
            failures.append(ValidationFailure(
                "no_number_words", f"{name} contains {sorted(overlap)}"
            ))

    # --- slots must be from the allowlist --------------------------------
    for name, text in fields.items():
        unknown = set(SLOT_PATTERN.findall(text)) - ALLOWED_SLOTS
        if unknown:
            failures.append(ValidationFailure(
                "slot_allowlist", f"{name} uses unknown slot(s) {sorted(unknown)}"
            ))

    # --- 2. the failure domain is named ----------------------------------
    if evidence.failure_domain and "{failure_domain}" not in everything:
        failures.append(ValidationFailure(
            "domain_named", "the attributed domain is never mentioned"
        ))

    # --- 2b. only domains present in the input ---------------------------
    known = {d for d in (evidence.failure_domain, evidence.runner_up) if d}
    for candidate in ("ordering-app", "promo-provider", "payment-gateway", "order-datastore"):
        if candidate in everything and candidate not in known:
            failures.append(ValidationFailure(
                "unknown_domain", f"names {candidate}, which is not in the evidence"
            ))

    # --- 3. an unaffected cohort is never described as affected ----------
    for cohort in evidence.unaffected_cohorts:
        if cohort in everything:
            failures.append(ValidationFailure(
                "unaffected_named", f"names the unaffected cohort {cohort!r} directly"
            ))

    # --- 6. uniform impact means no cohort is blamed ---------------------
    if evidence.uniform_impact and "{primary_cohort}" in everything:
        failures.append(ValidationFailure(
            "uniform_no_primary",
            "claims a primary cohort although impact was uniform",
        ))

    # --- 7/8. latency-only cohorts are slow, not failing ------------------
    latency_only = set(evidence.latency_affected_cohorts) - set(
        evidence.availability_affected_cohorts
    )
    if latency_only:
        failing_words = {"failing", "failed", "failure", "failures", "erroring", "errors"}
        for cohort in latency_only:
            if cohort in everything and _words(everything) & failing_words:
                failures.append(ValidationFailure(
                    "latency_only_not_failing",
                    f"{cohort!r} is slow, not failing, but the text says otherwise",
                ))

    # --- a primary cohort claim requires a primary dimension -------------
    if "{primary_cohort}" in everything and not evidence.primary_cohort:
        failures.append(ValidationFailure(
            "no_primary_to_name", "references a primary cohort that does not exist"
        ))

    return failures


def render(narrative: Narrative, evidence: NarrativeEvidence) -> dict[str, str]:
    """Substitute the slots. Runs only after validation passes.

    This is where numbers enter the text, from the evidence rather than from the
    model, which is the entire reason the model was forbidden to write them.
    """
    values = {
        "failure_domain": evidence.failure_domain or "an unidentified domain",
        "attribution_count": str(evidence.attribution_count),
        "candidate_count": str(evidence.candidate_count),
        "attribution_share": f"{evidence.attribution_share_pct:.0f}%",
        "affected_cohorts": _join(
            sorted(set(evidence.availability_affected_cohorts)
                   | set(evidence.latency_affected_cohorts))
        ),
        "unaffected_cohorts": _join(evidence.unaffected_cohorts),
        "runner_up": evidence.runner_up or "no other domain",
        "primary_dimension": evidence.primary_dimension or "no dimension",
        "primary_cohort": evidence.primary_cohort or "no cohort",
    }
    return {
        name: SLOT_PATTERN.sub(lambda m: values.get(m.group(1), m.group(0)), text)
        for name, text in narrative.fields().items()
    }


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return "none"
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" and {items[-1]}"
