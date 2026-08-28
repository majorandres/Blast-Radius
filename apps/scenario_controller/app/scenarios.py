"""Scenario definitions: what a fault is, and what the ground truth about it is.

A scenario is a name, an injected domain, a fault type, and a pair of fault
payloads — one for each service that has switches. Ground truth is derived from
the definition, so the recorded answer and the dispatched fault cannot drift
apart.

Scenario A is the only one required for MVP (v1.2 §14, §26).
"""

from dataclasses import dataclass, field
from typing import Literal

ScenarioName = Literal["A", "B", "C"]


@dataclass(frozen=True)
class Scenario:
    name: ScenarioName
    title: str
    #: The domain the fault is actually in. Written to `ground_truth` before any
    #: fault is dispatched, so a crash mid-injection cannot produce an
    #: unrecorded scenario.
    injected_domain: str
    fault_type: str
    #: Payload at full strength. The ramp scales these from zero.
    promo_faults: dict[str, float] = field(default_factory=dict)
    ordering_faults: dict[str, object] = field(default_factory=dict)


SCENARIO_A = Scenario(
    name="A",
    title="Third-party promo degradation",
    injected_domain="promo-provider",
    fault_type="dependency_degradation",
    # v1.2 §14.2. With the promo client timing out at 2000ms, most slow calls
    # become client-side aborts with no server span at all -- the CC-A path.
    promo_faults={"added_latency_ms": 3500, "timeout_prob": 0.30, "failure_prob": 0.0},
)

SCENARIO_B = Scenario(
    name="B",
    title="Partial payment degradation",
    injected_domain="payment-gateway",
    fault_type="partial_dependency_failure",
    ordering_faults={
        "payment": {"failure_prob": 0.55, "added_latency_ms": 200, "payment_method": "wallet"}
    },
)

SCENARIO_C = Scenario(
    name="C",
    title="Connection pool saturation",
    injected_domain="order-datastore",
    fault_type="resource_saturation",
    ordering_faults={"db": {"extra_concurrency": 25}},
)

SCENARIOS: dict[str, Scenario] = {s.name: s for s in (SCENARIO_A, SCENARIO_B, SCENARIO_C)}

#: Only Scenario A is wired end to end. B and C are defined so the dispatcher
#: shape is fixed, and are gated until their days.
IMPLEMENTED: frozenset[str] = frozenset({"A"})


def scale(payload: dict, factor: float) -> dict:
    """Scale a fault payload for the ramp.

    Probabilities and latencies scale linearly from zero to full strength.
    Non-numeric values (a payment method to target, say) pass through: they
    select *which* traffic is affected, not how hard.
    """
    scaled: dict = {}
    for key, value in payload.items():
        if isinstance(value, dict):
            scaled[key] = scale(value, factor)
        elif isinstance(value, bool) or not isinstance(value, (int, float)):
            scaled[key] = value
        else:
            scaled[key] = round(value * factor, 4) if isinstance(value, float) else int(
                round(value * factor)
            )
    return scaled
