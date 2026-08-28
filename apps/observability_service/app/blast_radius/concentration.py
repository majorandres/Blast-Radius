"""Concentration: does this cohort explain where the abnormality is (v1.2 §13.2).

A different question from impact, and the distinction is the whole point.

Under a wallet payment fault, every channel's failure rate rises — mobile, web,
and aggregator are all AFFECTED. But wallets exist across all channels, so no
channel *explains* anything; `payment_method=wallet` does. Impact answers "did
this cohort degrade". Concentration answers "does this cohort tell me where to
look". A cohort can be badly impacted and explain nothing.

v1.1 computed this over failures alone, gated behind a minimum failure count.
A fail-slow incident produces almost no failures, so every cohort returned
INSUFFICIENT_DATA on precisely the incident this analysis exists to characterise
as uniform. It is therefore computed over the §12.1 abnormal population — ERROR
**or** slower than the frozen threshold — which covers fail-fast and fail-slow
identically.
"""

from dataclasses import asdict, dataclass
from typing import Literal

from app.blast_radius.impact import CohortStats

Verdict = Literal["CONCENTRATED", "PROPORTIONAL", "SPARED", "INSUFFICIENT_DATA"]

CONCENTRATED_AT = 2.0
SPARED_AT = 0.5


@dataclass(frozen=True)
class CohortConcentration:
    dimension: str
    value: str
    traffic_share: float
    abnormal_share: float
    abnormal_n: int
    concentration_ratio: float | None
    verdict: Verdict

    def as_dict(self) -> dict:
        return asdict(self)


def concentration_of(
    cohort: CohortStats,
    *,
    total_traces: int,
    total_abnormal: int,
    min_cohort_n: int,
    min_abnormal_traces: int,
) -> tuple[float, float, float | None, Verdict]:
    """Returns (traffic_share, abnormal_share, ratio, verdict).

    The ratio is equivalently the cohort's abnormal rate divided by the
    system-wide abnormal rate, which is the more intuitive reading: 4x means
    this cohort goes wrong four times as often as traffic at large.
    """
    traffic_share = cohort.n / total_traces if total_traces else 0.0
    abnormal_share = cohort.abnormal_n / total_abnormal if total_abnormal else 0.0

    if cohort.n < min_cohort_n or total_abnormal < min_abnormal_traces:
        return traffic_share, abnormal_share, None, "INSUFFICIENT_DATA"
    if traffic_share == 0.0:
        return traffic_share, abnormal_share, None, "INSUFFICIENT_DATA"

    ratio = abnormal_share / traffic_share
    if ratio >= CONCENTRATED_AT:
        verdict: Verdict = "CONCENTRATED"
    elif ratio <= SPARED_AT:
        verdict = "SPARED"
    else:
        verdict = "PROPORTIONAL"
    return traffic_share, abnormal_share, ratio, verdict


def build_concentration(
    dimension: str,
    value: str,
    cohort: CohortStats,
    *,
    total_traces: int,
    total_abnormal: int,
    min_cohort_n: int,
    min_abnormal_traces: int,
) -> CohortConcentration:
    traffic_share, abnormal_share, ratio, verdict = concentration_of(
        cohort,
        total_traces=total_traces,
        total_abnormal=total_abnormal,
        min_cohort_n=min_cohort_n,
        min_abnormal_traces=min_abnormal_traces,
    )
    return CohortConcentration(
        dimension=dimension,
        value=value,
        traffic_share=traffic_share,
        abnormal_share=abnormal_share,
        abnormal_n=cohort.abnormal_n,
        concentration_ratio=ratio,
        verdict=verdict,
    )


def primary_dimension(
    concentrations: list[CohortConcentration],
) -> tuple[str | None, str | None]:
    """Which dimension, if any, separates the abnormal traces (v1.2 §13.3).

    A dimension qualifies only when its top cohort is CONCENTRATED *and* at
    least one sibling is SPARED. Requiring the spared sibling is what stops a
    dimension being called discriminating merely because one of its values is
    busy — without it, `channel=mobile` would win almost every incident on
    volume alone.

    Returning `None` is a positive finding, not a failure: it is how an
    infrastructure fault, which hits everyone evenly, is distinguished from a
    cohort-specific one.
    """
    by_dimension: dict[str, list[CohortConcentration]] = {}
    for c in concentrations:
        by_dimension.setdefault(c.dimension, []).append(c)

    best_dimension: str | None = None
    best_value: str | None = None
    best_ratio = 0.0

    for dimension, cohorts in by_dimension.items():
        if any(c.verdict == "INSUFFICIENT_DATA" for c in cohorts):
            continue
        rated = [c for c in cohorts if c.concentration_ratio is not None]
        if len(rated) < 2:
            continue
        top = max(rated, key=lambda c: c.concentration_ratio or 0.0)
        siblings = [c for c in rated if c is not top]
        if (top.concentration_ratio or 0.0) < CONCENTRATED_AT:
            continue
        if not any(s.verdict == "SPARED" for s in siblings):
            continue
        if (top.concentration_ratio or 0.0) > best_ratio:
            best_dimension, best_value = dimension, top.value
            best_ratio = top.concentration_ratio or 0.0

    return best_dimension, best_value
