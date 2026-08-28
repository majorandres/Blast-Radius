"""Red herring calibration (v1.2 §14.3).

The herrings are only useful if they actually fire. An earlier calibration
thresholded on `in_flight / capacity` and left both effectively switched off --
the detector then "defeated" herrings that were never present, which proves
nothing. These tests pin the calibration to the concurrency the system really
runs at.

Measured on the running system at 150 orders/min: smoothed in-flight 1.33-1.48
when healthy, 3.1-4.5 under a slow promo dependency.
"""

import random

import pytest

from app.dependencies.herrings import (
    IDLE_IN_FLIGHT,
    LOADED_IN_FLIGHT,
    LOADED_FAILURE_PROB,
    IDLE_FAILURE_PROB,
    analytics_failure_prob,
    load_factor,
    loyalty_delay_ms,
)
from app.traffic.load import SMOOTHING_ALPHA, LoadGauge

HEALTHY_SMOOTHED = 1.45
LOADED_SMOOTHED = 3.2


# --- the load curve --------------------------------------------------------
@pytest.mark.parametrize(
    ("smoothed", "expected"),
    [(0.0, 0.0), (1.0, 0.0), (IDLE_IN_FLIGHT, 0.0), (LOADED_IN_FLIGHT, 1.0), (9.0, 1.0)],
)
def test_load_factor_saturates_at_both_ends(smoothed, expected):
    assert load_factor(smoothed) == expected


def test_load_factor_is_monotonic():
    values = [load_factor(x / 10) for x in range(0, 60)]
    assert values == sorted(values)


def test_the_calibration_band_separates_healthy_from_loaded():
    """The whole point. If these overlap, the herrings either never fire or
    fire constantly, and both were observed while calibrating."""
    assert load_factor(HEALTHY_SMOOTHED) == 0.0, "healthy traffic must not trip a herring"
    assert load_factor(LOADED_SMOOTHED) == 1.0, "a slow dependency must trip both"
    assert IDLE_IN_FLIGHT < LOADED_IN_FLIGHT


# --- herring one: loyalty_tier_lookup --------------------------------------
def test_loyalty_is_about_8ms_healthy_and_about_45ms_loaded():
    """~5.6x relative, the largest rise in the system -- and about one percent
    of a multi-second trace, which is why it must never be the culprit."""
    rng = random.Random(0)
    healthy = [loyalty_delay_ms(rng, HEALTHY_SMOOTHED) for _ in range(400)]
    loaded = [loyalty_delay_ms(rng, LOADED_SMOOTHED) for _ in range(400)]

    healthy_mean = sum(healthy) / len(healthy)
    loaded_mean = sum(loaded) / len(loaded)

    assert 6.0 <= healthy_mean <= 10.0, healthy_mean
    assert 42.0 <= loaded_mean <= 48.0, loaded_mean
    assert loaded_mean / healthy_mean > 5.0


def test_loyalty_stays_a_rounding_error_against_a_degraded_trace():
    """45ms of a ~3500ms trace is ~1.3%, far under the 0.30 dominance floor.
    This is what makes a multiplier-based detector wrong and a share-based one
    right."""
    rng = random.Random(1)
    loaded = loyalty_delay_ms(rng, LOADED_SMOOTHED)
    assert loaded / 3500.0 < 0.02


# --- herring two: analytics.publish ----------------------------------------
def test_analytics_failure_rate_rises_from_one_percent_to_fifteen():
    assert analytics_failure_prob(HEALTHY_SMOOTHED) == pytest.approx(IDLE_FAILURE_PROB)
    assert analytics_failure_prob(LOADED_SMOOTHED) == pytest.approx(LOADED_FAILURE_PROB)


def test_analytics_failure_is_independent_of_the_checkout_outcome():
    """`analytics_failure_prob` takes no argument describing the checkout.

    That is the trap: a failing analytics publish says nothing about whether
    the order went through, so a detector keying on "deepest ERROR span" blames
    ordering-app on traces that succeeded.
    """
    import inspect

    parameters = inspect.signature(analytics_failure_prob).parameters
    assert list(parameters) == ["in_flight"]


# --- the gauge -------------------------------------------------------------
def test_smoothing_converges_to_sustained_concurrency():
    """`admit` counts the arriving checkout before smoothing, so with two
    already running the observed level is three."""
    gauge = LoadGauge()
    for _ in range(300):
        gauge.in_flight = 2
        gauge.admit()
        gauge.release()
    assert gauge.smoothed == pytest.approx(3.0, abs=0.1)


def test_a_single_burst_does_not_move_the_smoothed_mean_much():
    """A brief spike must not trip a herring; sustained pressure must."""
    gauge = LoadGauge()
    gauge.smoothed = 1.0
    gauge.in_flight = 8
    gauge.smoothed += SMOOTHING_ALPHA * (gauge.in_flight - gauge.smoothed)
    assert gauge.smoothed < 1.4, "one burst should barely register"


def test_release_does_not_disturb_the_smoothed_mean():
    gauge = LoadGauge()
    gauge.admit()
    before = gauge.smoothed
    gauge.release()
    assert gauge.smoothed == before
    assert gauge.in_flight == 0
