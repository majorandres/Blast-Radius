"""What the narrator is allowed to see, say, and be checked against (v1.2 §17).

The input is `NarrativeEvidence` and nothing else: no spans, no SQL, no ground
truth, no scenario identity. The model cannot leak what it was never given.

The output contains **no digits**. The model writes placeholders and the
renderer substitutes real values afterwards, which means a hallucinated number
is not a risk that has to be detected -- it is a sentence the model had no way
to write. Prompt injection is likewise not applicable here: every value in the
prompt is an enum or an integer from our own database, and there is no path for
external text to reach it.
"""

import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field


@dataclass(frozen=True)
class NarrativeEvidence:
    """The only input. Assembled from the incident's own analysis."""

    failure_domain: str | None
    failure_domain_kind: str | None
    verdict: str
    attribution_count: int
    candidate_count: int
    attribution_share_pct: float
    runner_up: str | None
    runner_up_share_pct: float | None
    symptoms: list[str] = field(default_factory=list)
    availability_affected_cohorts: list[str] = field(default_factory=list)
    latency_affected_cohorts: list[str] = field(default_factory=list)
    unaffected_cohorts: list[str] = field(default_factory=list)
    primary_dimension: str | None = None
    primary_cohort: str | None = None
    uniform_impact: bool = False
    dominant_path: Literal["error", "latency"] = "error"


class Narrative(BaseModel):
    """Three short fields, each at most two sentences (§17 rule 5)."""

    what_happened: str = Field(max_length=400)
    who_was_affected: str = Field(max_length=400)
    what_to_check: str = Field(max_length=400)

    def fields(self) -> dict[str, str]:
        return {
            "what_happened": self.what_happened,
            "who_was_affected": self.who_was_affected,
            "what_to_check": self.what_to_check,
        }


#: §17 rule 1. The only substitutions the model may write. Anything else in
#: braces is a slot it invented, and inventing a slot is how a fabricated fact
#: would get in.
ALLOWED_SLOTS: frozenset[str] = frozenset({
    "failure_domain",
    "attribution_count",
    "candidate_count",
    "attribution_share",
    "affected_cohorts",
    "unaffected_cohorts",
    "runner_up",
    "primary_dimension",
    "primary_cohort",
})

SLOT_PATTERN = re.compile(r"\{([a-z_]+)\}")

#: Digits and number words are both banned: "three domains" is as much a
#: fabricated quantity as "3 domains".
DIGIT_PATTERN = re.compile(r"\d")
NUMBER_WORDS = frozenset({
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "dozen", "half", "quarter", "third",
    "most", "majority", "few", "several", "many", "all", "none", "every",
})

SYSTEM_PROMPT = """\
You explain a production incident to an on-call engineer. You are given only a \
structured summary of what a detector concluded. You have no other information \
and must not invent any.

Rules, all of which are checked automatically:

1. Write NO digits and no number words. Use only these placeholders, which are \
substituted later: {failure_domain} {attribution_count} {candidate_count} \
{attribution_share} {affected_cohorts} {unaffected_cohorts} {runner_up} \
{primary_dimension} {primary_cohort}
2. Name only domains present in the input.
3. Never describe an unaffected cohort as affected.
4. Do not speculate beyond the stated failure domain.
5. Two sentences maximum per field.
6. When uniform_impact is true, name no cohort as primarily responsible.
7. Distinguish cohorts that were affected from the cohort that explains the \
concentration. These are different claims. Do not merge them.
8. Distinguish availability impact from latency impact. If a cohort appears \
only in latency_affected_cohorts, do not describe it as failing -- it is slow.

Write plainly, as a colleague would. No preamble, no reassurance, no advice \
beyond what the evidence supports."""
