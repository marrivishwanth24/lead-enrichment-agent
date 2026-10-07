"""
main.py — FastAPI app: submit leads, check pipeline status, review/approve drafts.

The background worker runs as an asyncio task started in the lifespan
handler — same process as the API for this project's scope. See worker.py
and the README for why that's a legitimate choice here and what the
production split looks like.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv

# Must run before any `from app...` import below -- pipeline.py constructs
# its ChatAnthropic clients at module import time, and that construction
# resolves ANTHROPIC_API_KEY from the environment immediately. Loading .env
# after that import would be too late: the key would already be missing.
# Railway/production isn't affected (env vars are injected into the process
# directly, no .env file involved), but any local `uvicorn app.main:app`
# run was silently broken without this -- found by actually running it
# locally, not assumed safe because production worked.
load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import db
from app.worker import worker_loop

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_stop_event = asyncio.Event()
_worker_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    global _worker_task
    _worker_task = asyncio.create_task(worker_loop(_stop_event))
    yield
    _stop_event.set()
    if _worker_task:
        await _worker_task


app = FastAPI(title="Lead Enrichment Agent", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")


class NewLead(BaseModel):
    company_name: str
    domain: str


class RejectBody(BaseModel):
    reason: str | None = None


@app.get("/")
async def dashboard():
    return FileResponse("static/index.html")


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.post("/leads", status_code=201)
async def submit_lead(body: NewLead):
    lead_id = await db.create_lead(body.company_name, body.domain)
    return {"id": lead_id, "status": "pending"}


@app.get("/leads/{lead_id}")
async def get_lead(lead_id: int):
    lead = await db.get_lead(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return lead


@app.get("/leads")
async def list_leads(status: str | None = None):
    if status is not None and status not in db.STATUSES:
        raise HTTPException(status_code=400, detail=f"Unknown status. Valid: {db.STATUSES}")
    return await db.list_leads(status)


@app.get("/drafts/pending")
async def pending_drafts():
    """Shortcut: leads with a draft that passed grading and awaits human approval."""
    return await db.list_leads(status="pending_approval")


@app.post("/leads/{lead_id}/approve")
async def approve_lead(lead_id: int):
    lead = await db.get_lead(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    if lead["status"] not in ("pending_approval", "flagged_for_review"):
        raise HTTPException(
            status_code=409,
            detail=f"Lead is '{lead['status']}', not awaiting approval",
        )
    await db.update_lead(lead_id, status="approved")
    return {"id": lead_id, "status": "approved"}


@app.post("/leads/{lead_id}/reject")
async def reject_lead(lead_id: int, body: RejectBody = RejectBody()):
    lead = await db.get_lead(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    if lead["status"] not in ("pending_approval", "flagged_for_review"):
        raise HTTPException(
            status_code=409,
            detail=f"Lead is '{lead['status']}', not awaiting approval",
        )
    await db.update_lead(lead_id, status="rejected", qualification_reason=body.reason or lead.get("qualification_reason"))
    return {"id": lead_id, "status": "rejected"}
