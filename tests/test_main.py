"""Tests for the FastAPI endpoints, against the isolated test DB.

Uses TestClient without entering it as a context manager, so the app's
lifespan (which starts the real background worker) never runs here -- these
tests exercise the HTTP/DB layer only, not the LLM pipeline. The worker's
own logic is covered separately in test_worker.py with mocks.
"""

from app import db
from app.main import app

client = None


def _client():
    global client
    if client is None:
        from fastapi.testclient import TestClient
        client = TestClient(app)
    return client


def test_health():
    r = _client().get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"


def test_submit_lead_returns_pending():
    r = _client().post("/leads", json={"company_name": "Acme", "domain": "acme.com"})
    assert r.status_code == 201
    assert r.json()["status"] == "pending"
    assert isinstance(r.json()["id"], int)


def test_get_lead_returns_full_record():
    created = _client().post("/leads", json={"company_name": "Acme", "domain": "acme.com"}).json()
    r = _client().get(f"/leads/{created['id']}")
    assert r.status_code == 200
    assert r.json()["company_name"] == "Acme"


def test_get_unknown_lead_404s():
    r = _client().get("/leads/999999")
    assert r.status_code == 404


def test_list_leads_rejects_unknown_status():
    r = _client().get("/leads", params={"status": "not-a-real-status"})
    assert r.status_code == 400


async def test_pending_drafts_only_returns_pending_approval():
    id1 = await db.create_lead("A", "a.com")
    await db.create_lead("B", "b.com")
    await db.update_lead(id1, status="pending_approval")

    r = _client().get("/drafts/pending")

    assert r.status_code == 200
    names = [lead["company_name"] for lead in r.json()]
    assert names == ["A"]


async def test_approve_lead_requires_pending_approval_status():
    lead_id = await db.create_lead("Acme", "acme.com")  # still 'pending', not awaiting approval

    r = _client().post(f"/leads/{lead_id}/approve")

    assert r.status_code == 409


async def test_approve_lead_succeeds_when_pending_approval():
    lead_id = await db.create_lead("Acme", "acme.com")
    await db.update_lead(lead_id, status="pending_approval")

    r = _client().post(f"/leads/{lead_id}/approve")

    assert r.status_code == 200
    assert r.json()["status"] == "approved"
    assert (await db.get_lead(lead_id))["status"] == "approved"


async def test_reject_lead_with_reason():
    lead_id = await db.create_lead("Acme", "acme.com")
    await db.update_lead(lead_id, status="flagged_for_review")

    r = _client().post(f"/leads/{lead_id}/reject", json={"reason": "not a real company"})

    assert r.status_code == 200
    lead = await db.get_lead(lead_id)
    assert lead["status"] == "rejected"
    assert lead["qualification_reason"] == "not a real company"


def test_approve_unknown_lead_404s():
    r = _client().post("/leads/999999/approve")
    assert r.status_code == 404
