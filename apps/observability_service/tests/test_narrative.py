"""Narrative generation, validation, and fallback (v1.2 §17, §21.5).

Every validator here guards against a failure that would be *plausible and
wrong*. A narrative naming an unaffected cohort as failing reads exactly as
fluently as a correct one, so fluency cannot be the check — which is why the
model is forbidden digits entirely and writes placeholders instead. A number it
cannot write is a number it cannot get wrong.
"""

import pytest

from app.narrative.contract import Narrative, NarrativeEvidence
from app.narrative.provider import StubProvider, build, fallback, runbook_for
from app.narrative.validate import render, validate

ATTRIBUTED = NarrativeEvidence(
    failure_domain="promo-provider",
    failure_domain_kind="process",
    verdict="ATTRIBUTED",
    attribution_count=42,
    candidate_count=45,
    attribution_share_pct=93.3,
    runner_up=None,
    runner_up_share_pct=None,
    symptoms=["checkout_success", "p95_latency"],
    availability_affected_cohorts=["with promotion", "mobile", "web"],
    latency_affected_cohorts=["with promotion", "mobile", "web"],
    unaffected_cohorts=["no promotion"],
    primary_dimension="has_promo",
    primary_cohort="with promotion",
    uniform_impact=False,
    dominant_path="error",
)

UNIFORM = NarrativeEvidence(
    failure_domain="order-datastore",
    failure_domain_kind="datastore",
    verdict="ATTRIBUTED",
    attribution_count=60,
    candidate_count=64,
    attribution_share_pct=93.8,
    runner_up=None,
    runner_up_share_pct=None,
    symptoms=["p95_latency"],
    availability_affected_cohorts=[],
    latency_affected_cohorts=["mobile", "web", "aggregator"],
    unaffected_cohorts=[],
    primary_dimension=None,
    primary_cohort=None,
    uniform_impact=True,
    dominant_path="latency",
)


def narrative(**overrides) -> Narrative:
    base = {
        "what_happened": "Traffic degraded and it was attributed to {failure_domain}.",
        "who_was_affected": "The affected cohorts were {affected_cohorts}.",
        "what_to_check": "Start with {primary_dimension}.",
    }
    return Narrative(**{**base, **overrides})


# --- the validators --------------------------------------------------------
def test_a_clean_narrative_passes():
    assert validate(narrative(), ATTRIBUTED) == []


@pytest.mark.parametrize("text", [
    "It affected 42 traces.",
    "About 90% of traffic degraded.",
])
def test_digits_are_rejected(text):
    failures = validate(narrative(who_was_affected=text), ATTRIBUTED)
    assert any(f.check == "no_digits" for f in failures)


@pytest.mark.parametrize("text", [
    "Three cohorts degraded.",
    "Most of the traffic was affected.",
    "All cohorts were hit.",
])
def test_number_words_are_rejected(text):
    """"Most of the traffic" is a quantity claim with no number in it, and it is
    exactly as unsupported as writing the figure."""
    failures = validate(narrative(who_was_affected=text), ATTRIBUTED)
    assert any(f.check == "no_number_words" for f in failures)


def test_an_invented_slot_is_rejected():
    """An invented slot renders as literal braces, but the real problem is that
    the model made up a fact it wanted filled in."""
    failures = validate(narrative(what_to_check="Check {database_host}."), ATTRIBUTED)
    assert any(f.check == "slot_allowlist" for f in failures)


def test_the_failure_domain_must_be_named():
    failures = validate(
        narrative(what_happened="Something degraded somewhere."), ATTRIBUTED
    )
    assert any(f.check == "domain_named" for f in failures)


def test_naming_a_domain_not_in_the_evidence_is_rejected():
    failures = validate(
        narrative(what_to_check="Also check payment-gateway."), ATTRIBUTED
    )
    assert any(f.check == "unknown_domain" for f in failures)


def test_an_unaffected_cohort_is_never_described():
    """§17 rule 3. Naming a spared cohort in prose is how a reader ends up
    believing it was hit."""
    failures = validate(
        narrative(who_was_affected="Checkouts with no promotion also degraded."),
        ATTRIBUTED,
    )
    assert any(f.check == "unaffected_named" for f in failures)


def test_uniform_impact_forbids_naming_a_primary_cohort():
    """§17 rule 6. Under an infrastructure fault there is no cohort to blame,
    and inventing one sends the reader somewhere there is nothing to find."""
    failures = validate(
        narrative(what_to_check="It centres on {primary_cohort}."), UNIFORM
    )
    assert any(f.check == "uniform_no_primary" for f in failures)


def test_a_latency_only_cohort_is_not_described_as_failing():
    """§17 rule 8, and the reason FINAL-01 split the verdicts. Under a
    fail-slow incident nothing is failing; saying otherwise sends an operator
    hunting for errors that do not exist."""
    failures = validate(
        narrative(
            what_happened="Requests through {failure_domain} degraded.",
            who_was_affected="mobile was failing throughout the incident.",
        ),
        UNIFORM,
    )
    assert any(f.check == "latency_only_not_failing" for f in failures)


# --- rendering -------------------------------------------------------------
def test_numbers_come_from_the_evidence_not_the_model():
    rendered = render(narrative(), ATTRIBUTED)
    assert "promo-provider" in rendered["what_happened"]
    assert "with promotion" in rendered["who_was_affected"]
    assert "has_promo" in rendered["what_to_check"]
    assert "{" not in "".join(rendered.values())


def test_rendering_lists_cohorts_readably():
    rendered = render(narrative(), ATTRIBUTED)
    assert " and " in rendered["who_was_affected"]


# --- the fallback ----------------------------------------------------------
def test_the_fallback_always_produces_a_valid_narrative():
    for evidence in (ATTRIBUTED, UNIFORM):
        rendered = fallback(evidence)
        assert set(rendered) == {"what_happened", "who_was_affected", "what_to_check"}
        assert all(v.strip() for v in rendered.values())
        assert "{" not in "".join(rendered.values())


def test_the_fallback_respects_uniform_impact():
    rendered = fallback(UNIFORM)
    text = " ".join(rendered.values())
    assert "shared infrastructure" in text
    assert "no cohort" not in text.replace("no cohort explains", "")


def test_the_fallback_is_honest_about_an_ambiguous_verdict():
    ambiguous = NarrativeEvidence(
        failure_domain="promo-provider", failure_domain_kind="process",
        verdict="AMBIGUOUS", attribution_count=20, candidate_count=45,
        attribution_share_pct=44.0, runner_up="payment-gateway",
        runner_up_share_pct=40.0,
    )
    rendered = fallback(ambiguous)
    assert "could not separate" in rendered["what_happened"]


# --- provider selection and the no-key path -------------------------------
async def test_with_no_provider_the_fallback_is_used_and_labelled():
    """§26: the app works with no API key. Not a degraded mode -- a supported
    one, and the label is what stops a template being mistaken for the model."""
    fields, source = build(ATTRIBUTED, provider=None)
    assert source == "fallback"
    assert "promo-provider" in fields["what_happened"]


async def test_the_stub_provider_produces_a_valid_narrative():
    fields, source = build(ATTRIBUTED, provider=StubProvider())
    assert source == "stub"
    assert "promo-provider" in fields["what_happened"]
    assert "{" not in "".join(fields.values())


async def test_a_provider_that_raises_falls_back_rather_than_failing():
    class Broken:
        name = "broken"

        def generate(self, evidence):
            raise RuntimeError("upstream is down")

    fields, source = build(ATTRIBUTED, provider=Broken())
    assert source == "fallback"
    assert fields["what_happened"]


async def test_a_provider_that_violates_a_rule_is_rejected_not_shown():
    """The whole point of validation: a fluent, confident, wrong narrative is
    replaced rather than surfaced."""
    class Liar:
        name = "liar"

        def generate(self, evidence):
            return Narrative(
                what_happened="{failure_domain} failed for 42 minutes.",
                who_was_affected="Checkouts with no promotion were worst hit.",
                what_to_check="Check {primary_dimension}.",
            )

    fields, source = build(ATTRIBUTED, provider=Liar())
    assert source == "fallback"
    assert "42" not in "".join(fields.values())
    assert "no promotion were worst hit" not in "".join(fields.values())


# --- runbooks --------------------------------------------------------------
def test_runbooks_are_keyed_by_domain_kind_and_never_generated():
    assert runbook_for("datastore")
    assert runbook_for("logical_dependency")
    assert runbook_for("process")
    assert runbook_for(None) == []
    assert all("pool" in step or "index" in step or "concurrency" in step
               for step in runbook_for("datastore"))
