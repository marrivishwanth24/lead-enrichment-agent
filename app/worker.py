"""
worker.py — background worker that claims pending leads and runs the pipeline.

Runs as an asyncio background task inside the same process as the API for
this project's scope (simplest thing that works for a demo). The claim/
process split (db.claim_next_pending() vs. process_one_lead()) is what
would let this become a genuinely separate worker process/deployment later
with no pipeline-logic changes — only db.py's polling query matters for
that split, not pipeline.py. See README for the production path (a
dedicated worker process, polling interval tuned to load, eventually a real
broker if throughput demands it).
"""

import asyncio
import logging

from app import db
from app.pipeline import run_pipeline

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 2.0


def _resolve_status(result: dict) -> str:
    """Map a finished pipeline run's state to a terminal lead status."""
    if result.get("error"):
        return "failed"
    if result.get("qualified") is False:
        return "disqualified"
    if result.get("grading_grounded") and result.get("grading_personalized"):
        return "pending_approval"
    return "flagged_for_review"  # drafted, but failed the grounding/personalization check


async def process_one_lead(lead: dict) -> None:
    """Run a single claimed lead through the pipeline and persist the outcome."""
    try:
        result = await run_pipeline(lead["company_name"], lead["domain"])
    except Exception as e:
        logger.exception("Pipeline crashed for lead %s", lead["id"])
        await db.update_lead(lead["id"], status="failed", error=str(e))
        return

    status = _resolve_status(result)
    await db.update_lead(
        lead["id"],
        status=status,
        raw_website_text=result.get("raw_website_text"),
        enrichment_summary=result.get("enrichment_summary"),
        qualified=result.get("qualified"),
        qualification_reason=result.get("qualification_reason"),
        draft_subject=result.get("draft_subject"),
        draft_body=result.get("draft_body"),
        grading_grounded=result.get("grading_grounded"),
        grading_personalized=result.get("grading_personalized"),
        grading_notes=result.get("grading_notes"),
        error=result.get("error"),
    )
    logger.info("Lead %s (%s) -> %s", lead["id"], lead["company_name"], status)


async def worker_loop(stop_event: asyncio.Event) -> None:
    """Poll for pending leads until stop_event is set."""
    logger.info("Worker started (poll interval: %.1fs)", POLL_INTERVAL_SECONDS)
    while not stop_event.is_set():
        lead = await db.claim_next_pending()
        if lead is None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
            continue
        await process_one_lead(lead)
    logger.info("Worker stopped")
