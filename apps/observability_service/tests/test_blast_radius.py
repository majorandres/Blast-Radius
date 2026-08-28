"""Impact and concentration (v1.2 §13, §21.1).

Impact answers *did this cohort degrade, and how*. Concentration answers *does
this cohort explain where the abnormality is*. They are different questions and
the tests keep them apart, including the case that makes the distinction matter:
a cohort that is badly impacted and explains nothing.
"""

import itertools

import pytest

from app.blast_radius.concentration import (
    CohortConcentration,
    build_concentration,
    concentration_of,
    primary_dimension,
)
from app.blast_radius.impact import (
    SEVERITY,
    CohortStats,
    availability_verdict,
    latency_verdict,
    overall_verdict,
)

MIN_N = 10
VERDICTS = ("AFFECTED", "DEGRADED", "UNAFFECTED", "INSUFFICIENT_DATA")


def stats(n: int, *, failure_rate=None, p95=None, abnormal_n=0) -> CohortStats:
    return CohortStats(n=n, failure_rate=failure_rate, p95_ms=p95, abnormal_n=abnormal_n)


# --- §13.1 availability ----------------------------------------------------
@pytest.mark.parametrize(
    ("base", "inc", "expected"),
    [
        (0.01, 0.01, "UNAFFECTED"),
        (0.01, 0.03, "UNAFFECTED"),   # +0.02 exactly: still unaffected
        (0.01, 0.031, "DEGRADED"),
        (0.01, 0.11, "AFFECTED"),     # base + 0.10 binds
        (0.10, 0.25, "DEGRADED"),     # clears base + 0.10 but not base * 3.0
        (0.10, 0.31, "AFFECTED"),     # base * 3.0 binds
    ],
)
def test_availability_verdict_boundaries(base, inc, expected):
    assert availability_verdict(base, 100, inc, 100, MIN_N) == expected


def test_availability_needs_both_windows_populated():
    assert availability_verdict(0.01, 3, 0.5, 100, MIN_N) == "INSUFFICIENT_DATA"
    assert availability_verdict(0.01, 100, 0.5, 3, MIN_N) == "INSUFFICIENT_DATA"


# --- §13.1 latency ---------------------------------------------------------
@pytest.mark.parametrize(
    ("base", "inc", "expected"),
    [
        (400.0, 400.0, "UNAFFECTED"),
        (400.0, 480.0, "UNAFFECTED"),   # base * 1.2 exactly
        (400.0, 481.0, "DEGRADED"),
        (400.0, 900.0, "AFFECTED"),     # base + 500 dominates base * 2
        (2000.0, 4000.0, "AFFECTED"),   # base * 2 dominates base + 500
        (2000.0, 3000.0, "DEGRADED"),
    ],
)
def test_latency_verdict_boundaries(base, inc, expected):
    assert latency_verdict(base, 100, inc, 100, MIN_N) == expected


def test_latency_requires_an_absolute_rise_not_only_a_multiplier():
    """A 20ms baseline tripling to 60ms is not an outage.

    Both bands use `max(multiplicative, absolute)`, so on a fast cohort the
    absolute term governs at each end: unaffected up to 70ms (20 + 50), and
    affected only from 520ms (20 + 500). Without those floors, a fast cohort
    would cry wolf over a rise nobody could perceive.
    """
    assert latency_verdict(20.0, 100, 60.0, 100, MIN_N) == "UNAFFECTED"
    assert latency_verdict(20.0, 100, 200.0, 100, MIN_N) == "DEGRADED"
    assert latency_verdict(20.0, 100, 521.0, 100, MIN_N) == "AFFECTED"


# --- §13.1 overall, derived ------------------------------------------------
def test_overall_verdict_is_the_worse_of_the_two_known_verdicts():
    for availability, latency in itertools.product(VERDICTS, VERDICTS):
        result = overall_verdict(availability, latency)
        known = [v for v in (availability, latency) if v != "INSUFFICIENT_DATA"]
        expected = (
            "INSUFFICIENT_DATA" if not known
            else max(known, key=lambda v: SEVERITY[v])
        )
        assert result == expected, f"{availability} + {latency}"


def test_overall_covers_all_sixteen_combinations():
    assert len(list(itertools.product(VERDICTS, VERDICTS))) == 16


def test_a_slow_but_not_failing_cohort_is_affected_overall():
    """The fail-slow case. Under v1.1 this cohort read UNAFFECTED, which is the
    exact failure FINAL-01 exists to fix."""
    assert overall_verdict("UNAFFECTED", "AFFECTED") == "AFFECTED"


def test_insufficient_availability_does_not_mask_a_latency_finding():
    assert overall_verdict("INSUFFICIENT_DATA", "AFFECTED") == "AFFECTED"


# --- §13.2 concentration ---------------------------------------------------
def test_concentration_ratio_is_abnormal_share_over_traffic_share():
    cohort = stats(350, abnormal_n=100)
    _, _, ratio, verdict = concentration_of(
        cohort, total_traces=1000, total_abnormal=100,
        min_cohort_n=MIN_N, min_abnormal_traces=MIN_N,
    )
    # 35% of traffic, 100% of the abnormality.
    assert ratio == pytest.approx(1.0 / 0.35, rel=1e-3)
    assert verdict == "CONCENTRATED"


@pytest.mark.parametrize(
    ("cohort_n", "cohort_abnormal", "expected"),
    [
        (500, 100, "CONCENTRATED"),   # 50% traffic, 100% abnormal -> 2.0
        (500, 50, "PROPORTIONAL"),    # 50% traffic, 50% abnormal  -> 1.0
        (500, 25, "SPARED"),          # 50% traffic, 25% abnormal  -> 0.5
        (500, 0, "SPARED"),
    ],
)
def test_concentration_verdict_boundaries(cohort_n, cohort_abnormal, expected):
    cohort = stats(cohort_n, abnormal_n=cohort_abnormal)
    *_, verdict = concentration_of(
        cohort, total_traces=1000, total_abnormal=100,
        min_cohort_n=MIN_N, min_abnormal_traces=MIN_N,
    )
    assert verdict == expected


def test_concentration_works_with_zero_failures_and_many_slow_traces():
    """The fixture FINAL-02 exists for.

    Nothing failed. Everything is slow. v1.1 counted failures and returned
    INSUFFICIENT_DATA everywhere, on precisely the incident that needed
    characterising as uniform. Over the abnormal population it works.
    """
    cohorts = {
        "mobile": stats(550, failure_rate=0.0, p95=2400.0, abnormal_n=520),
        "web": stats(350, failure_rate=0.0, p95=2380.0, abnormal_n=330),
        "aggregator": stats(100, failure_rate=0.0, p95=2410.0, abnormal_n=95),
    }
    total_traces = sum(c.n for c in cohorts.values())
    total_abnormal = sum(c.abnormal_n for c in cohorts.values())

    results = [
        build_concentration("channel", value, cohort, total_traces=total_traces,
                            total_abnormal=total_abnormal, min_cohort_n=MIN_N,
                            min_abnormal_traces=MIN_N)
        for value, cohort in cohorts.items()
    ]

    assert all(r.verdict == "PROPORTIONAL" for r in results), [r.verdict for r in results]
    assert all(r.abnormal_n > 0 for r in results)
    assert primary_dimension(results) == (None, None), "uniform: no cohort explains it"


def test_a_thin_cohort_is_insufficient_data_not_spared():
    cohort = stats(3, abnormal_n=0)
    *_, verdict = concentration_of(
        cohort, total_traces=1000, total_abnormal=100,
        min_cohort_n=MIN_N, min_abnormal_traces=MIN_N,
    )
    assert verdict == "INSUFFICIENT_DATA"


def test_too_few_abnormal_traces_overall_is_insufficient_data():
    cohort = stats(500, abnormal_n=2)
    *_, verdict = concentration_of(
        cohort, total_traces=1000, total_abnormal=3,
        min_cohort_n=MIN_N, min_abnormal_traces=MIN_N,
    )
    assert verdict == "INSUFFICIENT_DATA"


# --- §13.3 primary dimension ----------------------------------------------
def concentration(dimension: str, value: str, ratio: float, verdict: str) -> CohortConcentration:
    return CohortConcentration(
        dimension=dimension, value=value, traffic_share=0.5, abnormal_share=0.5,
        abnormal_n=50, concentration_ratio=ratio, verdict=verdict,
    )


def test_primary_dimension_requires_a_spared_sibling():
    """Without this, a dimension is called discriminating merely because one of
    its values is busy — mobile would win nearly every incident on volume."""
    without_spared = [
        concentration("channel", "mobile", 2.4, "CONCENTRATED"),
        concentration("channel", "web", 0.8, "PROPORTIONAL"),
        concentration("channel", "aggregator", 0.7, "PROPORTIONAL"),
    ]
    assert primary_dimension(without_spared) == (None, None)

    with_spared = [
        concentration("channel", "mobile", 2.4, "CONCENTRATED"),
        concentration("channel", "web", 0.8, "PROPORTIONAL"),
        concentration("channel", "aggregator", 0.2, "SPARED"),
    ]
    assert primary_dimension(with_spared) == ("channel", "mobile")


def test_the_strongest_discriminating_dimension_wins():
    cohorts = [
        concentration("channel", "mobile", 2.1, "CONCENTRATED"),
        concentration("channel", "web", 0.3, "SPARED"),
        concentration("has_promo", "true", 2.9, "CONCENTRATED"),
        concentration("has_promo", "false", 0.0, "SPARED"),
    ]
    assert primary_dimension(cohorts) == ("has_promo", "true")


def test_a_dimension_with_insufficient_data_is_skipped():
    cohorts = [
        concentration("payment_method", "wallet", 4.0, "CONCENTRATED"),
        concentration("payment_method", "card", 0.0, "SPARED"),
        concentration("payment_method", "other", 0.0, "INSUFFICIENT_DATA"),
    ]
    assert primary_dimension(cohorts) == (None, None)


def test_impact_and_concentration_are_not_the_same_claim():
    """v1.2 §13.4, the wallet case.

    Every channel is AFFECTED, because wallets exist across all of them. No
    channel explains anything; payment_method does. A cohort can be badly
    impacted and explain nothing, and conflating the two is the mistake this
    whole split exists to prevent.
    """
    channels = [
        concentration("channel", "mobile", 1.0, "PROPORTIONAL"),
        concentration("channel", "web", 1.0, "PROPORTIONAL"),
        concentration("channel", "aggregator", 1.0, "PROPORTIONAL"),
    ]
    payments = [
        concentration("payment_method", "wallet", 4.0, "CONCENTRATED"),
        concentration("payment_method", "card", 0.0, "SPARED"),
        concentration("payment_method", "other", 0.0, "SPARED"),
    ]

    for channel in channels:
        assert overall_verdict("AFFECTED", "UNAFFECTED") == "AFFECTED"
        assert channel.verdict == "PROPORTIONAL"

    assert primary_dimension(channels + payments) == ("payment_method", "wallet")
