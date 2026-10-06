"""Tests for db.py. Each test gets an isolated temp SQLite file via the
autouse _isolated_db fixture in conftest.py."""

import pytest

from app import db


@pytest.mark.asyncio
async def test_create_and_get_lead():
    lead_id = await db.create_lead("Acme Corp", "acme.com")
    lead = await db.get_lead(lead_id)

    assert lead["company_name"] == "Acme Corp"
    assert lead["domain"] == "acme.com"
    assert lead["status"] == "pending"


@pytest.mark.asyncio
async def test_get_lead_returns_none_for_unknown_id():
    assert await db.get_lead(99999) is None


@pytest.mark.asyncio
async def test_list_leads_filters_by_status():
    id1 = await db.create_lead("A", "a.com")
    await db.create_lead("B", "b.com")
    await db.update_lead(id1, status="approved")

    pending = await db.list_leads(status="pending")
    approved = await db.list_leads(status="approved")

    assert len(pending) == 1 and pending[0]["company_name"] == "B"
    assert len(approved) == 1 and approved[0]["company_name"] == "A"


@pytest.mark.asyncio
async def test_claim_next_pending_flips_status_and_returns_oldest():
    id1 = await db.create_lead("First", "first.com")
    await db.create_lead("Second", "second.com")

    claimed = await db.claim_next_pending()

    assert claimed["id"] == id1
    assert claimed["status"] == "enriching"
    # Persisted, not just returned in-memory:
    assert (await db.get_lead(id1))["status"] == "enriching"


@pytest.mark.asyncio
async def test_claim_next_pending_returns_none_when_nothing_pending():
    assert await db.claim_next_pending() is None


@pytest.mark.asyncio
async def test_claim_next_pending_does_not_reclaim_already_claimed():
    await db.create_lead("Only", "only.com")
    first_claim = await db.claim_next_pending()
    second_claim = await db.claim_next_pending()

    assert first_claim is not None
    assert second_claim is None


@pytest.mark.asyncio
async def test_update_lead_json_encodes_dict_values():
    lead_id = await db.create_lead("Acme", "acme.com")
    await db.update_lead(lead_id, enrichment_summary={"what_they_do": "widgets"})

    lead = await db.get_lead(lead_id)
    assert '"what_they_do"' in lead["enrichment_summary"]


@pytest.mark.asyncio
async def test_update_lead_with_no_fields_is_a_safe_noop():
    lead_id = await db.create_lead("Acme", "acme.com")
    await db.update_lead(lead_id)  # must not raise
    assert (await db.get_lead(lead_id))["status"] == "pending"
