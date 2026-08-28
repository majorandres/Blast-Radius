"""Idempotent span persistence and trace-head denormalization (v1.2 §7.2).

Two statements. The first inserts spans and folds the newly-inserted count into
the trace head; the second promotes a newly-arrived root span's fields onto that
head.

The idempotence matters more than it looks. A fully duplicate batch produces
zero rows in `agg`, so `last_span_ts` does not advance and the Day 2 settle gate
is not delayed. Re-delivery therefore costs nothing and stalls nothing.

Transaction dimensions are denormalized onto `trace` from the root span's
attributes at ingest, so blast radius never reads `"order"` -- which the detector
role cannot read anyway, and which is missing precisely for the worst-affected
transactions when persistence fails (RC4).
"""

import json
from datetime import UTC, datetime

import sqlalchemy as sa
from blastradius_contracts.telemetry import SpanEnvelope
from sqlalchemy.ext.asyncio import AsyncConnection

SPAN_COLUMNS = (
    "trace_id", "span_id", "parent_span_id", "emitting_service_id",
    "attribution_domain_id", "span_kind", "operation", "start_ts", "end_ts",
    "duration_ms", "status", "blocking", "attributes",
)

# Enum and jsonb parameters need explicit casts; every value is bound, never
# interpolated (v1.2 §23).
_VALUE_TEMPLATE = (
    "(:trace_id_{i}, :span_id_{i}, :parent_span_id_{i}, :emitting_service_id_{i},"
    " :attribution_domain_id_{i}, CAST(:span_kind_{i} AS span_kind), :operation_{i},"
    " :start_ts_{i}, :end_ts_{i}, :duration_ms_{i}, CAST(:status_{i} AS span_status),"
    " :blocking_{i}, CAST(:attributes_{i} AS jsonb))"
)

_INSERT_SPANS = """
WITH ins AS (
  INSERT INTO span ({columns})
  VALUES {values}
  ON CONFLICT (trace_id, span_id) DO NOTHING
  RETURNING trace_id
),
agg AS (
  SELECT trace_id, count(*) AS new_spans FROM ins GROUP BY trace_id
)
INSERT INTO trace (trace_id, span_count, last_span_ts)
SELECT trace_id, new_spans, now() FROM agg
ON CONFLICT (trace_id) DO UPDATE SET
  span_count   = trace.span_count + EXCLUDED.span_count,
  last_span_ts = EXCLUDED.last_span_ts
"""

# Restricted to this batch's traces. Equivalent to the unrestricted form in
# v1.2 §7.2 -- a root can only become newly visible in the batch that carried
# it -- but bounded instead of scanning every rootless trace on every batch.
_POPULATE_ROOT = """
UPDATE trace t SET
  root_span_id     = s.span_id,
  root_operation   = s.operation,
  root_status      = s.status,
  root_start_ts    = s.start_ts,
  root_end_ts      = s.end_ts,
  root_duration_ms = s.duration_ms,
  order_id         = (s.attributes->>'order.id')::uuid,
  channel          =  s.attributes->>'order.channel',
  has_promo        = (s.attributes->>'order.has_promo')::boolean,
  payment_method   =  s.attributes->>'order.payment_method',
  checkout_status  = CASE WHEN s.status = 'ERROR' THEN 'FAILED'::checkout_status
                          ELSE 'CONFIRMED'::checkout_status END
FROM span s
WHERE s.trace_id = t.trace_id
  AND s.parent_span_id IS NULL
  AND t.root_span_id IS NULL
  AND t.trace_id = ANY(:trace_ids)
"""


def to_datetime(unix_nano: int) -> datetime:
    return datetime.fromtimestamp(unix_nano / 1e9, tz=UTC)


def _params(
    spans: list[SpanEnvelope], service_ids: dict[str, int], domain_ids: dict[str, int]
) -> dict[str, object]:
    params: dict[str, object] = {}
    for i, s in enumerate(spans):
        start, end = to_datetime(s.start_unix_nano), to_datetime(s.end_unix_nano)
        params |= {
            f"trace_id_{i}": s.trace_id,
            f"span_id_{i}": s.span_id,
            f"parent_span_id_{i}": s.parent_span_id,
            f"emitting_service_id_{i}": service_ids[s.emitting_service],
            f"attribution_domain_id_{i}": domain_ids[s.attribution_domain],
            f"span_kind_{i}": s.span_kind,
            f"operation_{i}": s.operation,
            f"start_ts_{i}": start,
            f"end_ts_{i}": end,
            f"duration_ms_{i}": max(0, round((s.end_unix_nano - s.start_unix_nano) / 1e6)),
            f"status_{i}": s.status,
            f"blocking_{i}": s.blocking,
            f"attributes_{i}": json.dumps(s.attributes),
        }
    return params


async def write_spans(
    conn: AsyncConnection,
    spans: list[SpanEnvelope],
    service_ids: dict[str, int],
    domain_ids: dict[str, int],
) -> int:
    if not spans:
        return 0

    values = ", ".join(_VALUE_TEMPLATE.format(i=i) for i in range(len(spans)))
    statement = _INSERT_SPANS.format(columns=", ".join(SPAN_COLUMNS), values=values)
    await conn.execute(sa.text(statement), _params(spans, service_ids, domain_ids))

    await conn.execute(
        sa.text(_POPULATE_ROOT), {"trace_ids": sorted({s.trace_id for s in spans})}
    )
    await conn.commit()
    return len(spans)
