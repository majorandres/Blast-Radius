"""Scenario A acceptance (v1.2 §14.5) — the whole loop, blind.

This drives a real injection through `scenario-controller` and then reads what
the detector concluded, entirely from its own side of the trust boundary. The
detector is not told a scenario is running, cannot read `ground_truth`, and has
no route to the controller at all.

It is slow by nature: the fault ramps, the SLO window fills, and the incident
has to open. Marked `slow` so it can be deselected with `-m "not slow"` during
inner-loop work, but it runs by default because it is the acceptance test.
"""

import asyncio
import os
from datetime import datetime

import pytest
import pytest_asyncio
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.slow

CONTROLLER_URL = os.environ.get("SCENARIO_CONTROLLER_URL", "http://scenario-controller:8003")
DETECTOR_URL = os.environ.get(
    "DATABASE_URL_DETECTOR",
    "postgresql+asyncpg://blastradius_detector:detector@postgres:5432/blastradius",
)

#: DEMO: ramp 15s, then the 60s SLO window has to fill enough to breach twice.
DIAGNOSIS_TIMEOUT_S = 200

#: Read the incident once it is well established, not at the first sign of one.
#: §14.4's expectations describe the scenario at full fault strength; sampling
#: 20 candidates in means sampling mid-ramp, where the promo cohort is
#: genuinely only DEGRADED and the test would be asserting a timing accident.
MIN_CANDIDATES = 60

#: The baseline window looks back BASELINE_WINDOW_S..BASELINE_GUARD_S before the
#: first breach and is frozen at open. If a previous run's fault is still inside
#: it, the incident is measured against a degraded baseline and the verdicts are
#: wrong in a specific direction: with `base_rate = 0.41`, AFFECTED would demand
#: an incident failure rate of 1.22, which no cohort can reach. So the fixture
#: waits for a genuinely healthy stretch before injecting.
BASELINE_WINDOW_S = 240
HEALTHY_BASELINE_MAX_FAILURE_RATE = 0.02
BASELINE_SETTLE_TIMEOUT_S = 300

_BASELINE_HEALTH = sa.text(
    """
    SELECT count(*) AS n,
           count(*) FILTER (WHERE checkout_status = 'FAILED')::numeric
             / NULLIF(count(*), 0) AS failure_rate
    FROM trace
    WHERE root_span_id IS NOT NULL
      AND root_end_ts >= now() - make_interval(secs => :window_s)
    """
)

_LIVE_INCIDENTS = sa.text(
    "SELECT count(*) FROM incident"
    " WHERE state::text = ANY(ARRAY['PENDING','OPEN','RECOVERING'])"
)


async def _wait_for_healthy_baseline(engine) -> tuple[float, int]:
    """Block until the last BASELINE_WINDOW_S of traffic is genuinely healthy."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + BASELINE_SETTLE_TIMEOUT_S
    rate = 1.0
    live_incidents = 1
    while loop.time() < deadline:
        async with engine.connect() as conn:
            row = (await conn.execute(
                _BASELINE_HEALTH, {"window_s": BASELINE_WINDOW_S}
            )).mappings().one()
            live_incidents = int((await conn.execute(_LIVE_INCIDENTS)).scalar_one() or 0)
        rate = float(row["failure_rate"] or 0.0)
        if (
            int(row["n"] or 0) > 50
            and rate <= HEALTHY_BASELINE_MAX_FAILURE_RATE
            and live_incidents == 0
        ):
            return rate, live_incidents
        await asyncio.sleep(5)
    return rate, live_incidents
_INCIDENT = sa.text(
    """
    SELECT i.verdict::text AS verdict, d.name AS attributed_domain,
           i.attribution_share, i.candidate_trace_count, i.attribution_detail,
           i.impact, i.concentration, i.primary_dimension, i.primary_cohort,
           i.state::text AS state, i.first_breach_ts
    FROM incident i LEFT JOIN domain d ON d.id = i.attributed_domain_id
    ORDER BY i.first_breach_ts DESC LIMIT 1
    """
)


async def _wait_for(engine, predicate, timeout_s: int):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    row = None
    while loop.time() < deadline:
        async with engine.connect() as conn:
            row = (await conn.execute(_INCIDENT)).mappings().first()
        if predicate(row):
            return row
        await asyncio.sleep(3)
    return row


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def diagnosis():
    """Inject Scenario A blind, then read the detector's conclusion."""
    engine = create_async_engine(DETECTOR_URL)
    async with AsyncClient(timeout=30) as client:
        try:
            await client.post(f"{CONTROLLER_URL}/api/reset")

            baseline_rate, live_incidents = await _wait_for_healthy_baseline(engine)
            assert baseline_rate <= HEALTHY_BASELINE_MAX_FAILURE_RATE, (
                f"baseline window still shows {baseline_rate:.2%} failures after "
                f"{BASELINE_SETTLE_TIMEOUT_S}s; the incident would be measured "
                "against a degraded baseline"
            )
            assert live_incidents == 0, (
                f"{live_incidents} incident(s) still live; a new one cannot form"
            )

            response = await client.post(
                f"{CONTROLLER_URL}/api/scenarios/inject",
                json={"mode": "blind", "scenario": "A", "seed": 4242},
            )
            response.raise_for_status()
            run = response.json()
            started = datetime.fromisoformat(run["started_ts"])

            # The incident must have begun *after* this run started. Without
            # this the fixture happily reads a closed incident from an earlier
            # run and the whole acceptance test passes in seconds against stale
            # evidence. It is the §5.4 association rule, and it belongs here for
            # the same reason it belongs in reveal.
            def diagnosed(r) -> bool:
                return (
                    r is not None
                    and r["first_breach_ts"] is not None
                    and r["first_breach_ts"] >= started
                    and r["verdict"] is not None
                    and (r["candidate_trace_count"] or 0) >= MIN_CANDIDATES
                )

            row = await _wait_for(engine, diagnosed, DIAGNOSIS_TIMEOUT_S)
            assert row is not None and diagnosed(row), (
                "no incident attributable to this run appeared within "
                f"{DIAGNOSIS_TIMEOUT_S}s"
            )
            yield run, row
        finally:
            await client.post(f"{CONTROLLER_URL}/api/reset")
            await engine.dispose()


def _cohort(rows, dimension, value):
    return next(r for r in rows if r["dimension"] == dimension and r["value"] == value)


@pytest.mark.asyncio(loop_scope="module")
async def test_the_scenario_is_withheld_in_blind_mode(diagnosis):
    """The frontend holds this response. If it named the scenario, there would
    be nothing to diagnose."""
    run, _ = diagnosis
    assert run["mode"] == "blind"
    assert run["scenario"] is None


@pytest.mark.asyncio(loop_scope="module")
async def test_the_detector_attributes_the_promo_provider(diagnosis):
    _, row = diagnosis
    assert row is not None, "no incident was opened"
    assert row["verdict"] == "ATTRIBUTED"
    assert row["attributed_domain"] == "promo-provider"
    assert float(row["attribution_share"]) >= 0.70


@pytest.mark.asyncio(loop_scope="module")
async def test_the_ordering_app_is_not_blamed(diagnosis):
    """Herring two. `analytics.publish` fails at up to 15% under exactly this
    load, and it is emitted by ordering-app. A detector taking the deepest
    ERROR span anywhere blames ordering-app here."""
    _, row = diagnosis
    assert row["attributed_domain"] != "ordering-app"


@pytest.mark.asyncio(loop_scope="module")
async def test_loyalty_tier_lookup_is_never_the_culprit(diagnosis):
    """Herring one. It has the largest relative rise in the system -- ~8ms to
    ~45ms -- and is about one percent of a degraded trace."""
    _, row = diagnosis
    operations = row["attribution_detail"].get("culprit_operations", {})
    assert operations, "no culprit operations recorded"
    assert "loyalty_tier_lookup" not in operations


@pytest.mark.asyncio(loop_scope="module")
async def test_the_client_span_path_is_exercised(diagnosis):
    """CC-A. The promo call aborts at its 2000ms client timeout, so no server
    span exists and the culprit is a CLIENT span carrying its peer's domain."""
    _, row = diagnosis
    kinds = row["attribution_detail"].get("culprit_kinds", {})
    assert kinds.get("CLIENT", 0) > 0


@pytest.mark.asyncio(loop_scope="module")
async def test_promotion_status_explains_the_concentration(diagnosis):
    _, row = diagnosis
    assert row["primary_dimension"] == "has_promo"
    assert row["primary_cohort"] == "true"


@pytest.mark.asyncio(loop_scope="module")
async def test_promo_traffic_is_affected_and_non_promo_traffic_is_not(diagnosis):
    _, row = diagnosis
    promo = _cohort(row["impact"], "has_promo", "true")
    clean = _cohort(row["impact"], "has_promo", "false")

    detail = (
        f"promo n={promo['incident_n']} fail={promo['incident_failure_rate']} "
        f"p95={promo['incident_p95_ms']} (baseline fail={promo['baseline_failure_rate']}, "
        f"p95={promo['baseline_p95_ms']}); clean n={clean['incident_n']} "
        f"fail={clean['incident_failure_rate']} p95={clean['incident_p95_ms']}"
    )
    assert promo["availability_verdict"] == "AFFECTED", detail
    assert promo["overall_verdict"] == "AFFECTED", detail
    assert clean["overall_verdict"] == "UNAFFECTED", detail


@pytest.mark.asyncio(loop_scope="module")
async def test_concentration_separates_promo_from_non_promo(diagnosis):
    """The claim impact cannot make: promotion status is what *explains* where
    the abnormality is, not merely what was hit."""
    _, row = diagnosis
    promo = _cohort(row["concentration"], "has_promo", "true")
    clean = _cohort(row["concentration"], "has_promo", "false")

    assert promo["verdict"] == "CONCENTRATED"
    assert clean["verdict"] == "SPARED"
    assert promo["concentration_ratio"] >= 2.0


@pytest.mark.asyncio(loop_scope="module")
async def test_channels_are_hit_but_explain_nothing(diagnosis):
    """Promo orders spread across every channel, so channels are impacted while
    explaining nothing. Conflating those two claims is the mistake the
    impact/concentration split exists to prevent.

    The claim asserted is that no channel *discriminates*: none is CONCENTRATED,
    and the well-populated ones are PROPORTIONAL. `aggregator` is only ~10% of
    traffic and can legitimately read INSUFFICIENT_DATA early in an incident --
    at 150 orders/min a 60s window holds ~15 aggregator traces against a floor
    of 10, which is exactly the thin-cohort case CC-C anticipates. Demanding
    PROPORTIONAL there would be asserting a timing coincidence, not a finding.
    """
    _, row = diagnosis
    channels = {c["value"]: c["verdict"] for c in row["concentration"]
                if c["dimension"] == "channel"}
    assert channels

    assert not any(v == "CONCENTRATED" for v in channels.values()), channels
    for busy in ("mobile", "web"):
        assert channels[busy] == "PROPORTIONAL", channels
