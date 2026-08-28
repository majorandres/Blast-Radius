"""POST /internal/spans.

Returns 202 with `{accepted, fenced}`. Unknown service or domain names are a
400: the emitters and this service share `packages/contracts`, so an unknown
name means a genuine contract violation, not a transient condition.

Note what this endpoint does *not* accept. There is no scenario id, no run
state, no header or field carrying anything about whether a fault is active.
The detector cannot learn that a scenario exists (v1.2 §1.3).
"""

import logging

from blastradius_contracts.telemetry import SpanBatch
from fastapi import APIRouter, HTTPException, Request, status

from app.db import engine
from app.ingest.fence import fence
from app.ingest.writer import to_datetime, write_spans

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/internal/spans", status_code=status.HTTP_202_ACCEPTED)
async def ingest_spans(batch: SpanBatch, request: Request) -> dict[str, int]:
    service_ids: dict[str, int] = request.app.state.service_ids
    domain_ids: dict[str, int] = request.app.state.domain_ids

    kept, fenced_count = [], 0
    for span in batch.spans:
        if fence.is_fenced(to_datetime(span.end_unix_nano)):
            fenced_count += 1
            continue
        if span.emitting_service not in service_ids:
            raise HTTPException(400, f"unknown emitting_service: {span.emitting_service}")
        if span.attribution_domain not in domain_ids:
            raise HTTPException(400, f"unknown attribution_domain: {span.attribution_domain}")
        kept.append(span)

    if fenced_count:
        fence.record_fenced(fenced_count)
        log.info("fenced %s spans older than last_reset_ts=%s", fenced_count,
                 fence.last_reset_ts.isoformat())

    async with engine().connect() as conn:
        accepted = await write_spans(conn, kept, service_ids, domain_ids)

    return {"accepted": accepted, "fenced": fenced_count}
