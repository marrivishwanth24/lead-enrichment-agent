"""Tests for worker.py's status-resolution logic and lead processing."""

from unittest.mock import AsyncMock, patch

import pytest

from app import db
from app.worker import _resolve_status, process_one_lead


def test_resolve_status_error_takes_priority():
    assert _resolve_status({"error": "boom", "qualified": True}) == "failed"


def test_resolve_status_disqualified():
    assert _resolve_status({"qualified": False}) == "disqualified"


def test_resolve_status_pending_approval_when_grounded_and_personalized():
    result = {"qualified": True, "grading_grounded": True, "grading_personalized": True}
    assert _resolve_status(result) == "pending_approval"


def test_resolve_status_flagged_when_not_grounded():
    result = {"qualified": True, "grading_grounded": False, "grading_personalized": True}
    assert _resolve_status(result) == "flagged_for_review"


def test_resolve_status_flagged_when_not_personalized():
    result = {"qualified": True, "grading_grounded": True, "grading_personalized": False}
    assert _resolve_status(result) == "flagged_for_review"


@pytest.mark.asyncio
async def test_process_one_lead_persists_successful_outcome():
    lead_id = await db.create_lead("Acme", "acme.com")
    lead = await db.get_lead(lead_id)
    fake_result = {
        "enrichment_summary": {"what_they_do": "widgets"},
        "qualified": True,
        "qualification_reason": "good fit",
        "draft_subject": "Hi",
        "draft_body": "body",
        "grading_grounded": True,
        "grading_personalized": True,
        "grading_notes": "ok",
    }

    with patch("app.worker.run_pipeline", new=AsyncMock(return_value=fake_result)):
        await process_one_lead(lead)

    updated = await db.get_lead(lead_id)
    assert updated["status"] == "pending_approval"
    assert updated["draft_subject"] == "Hi"


@pytest.mark.asyncio
async def test_process_one_lead_handles_pipeline_crash_without_raising():
    lead_id = await db.create_lead("Acme", "acme.com")
    lead = await db.get_lead(lead_id)

    with patch("app.worker.run_pipeline", new=AsyncMock(side_effect=RuntimeError("crash"))):
        await process_one_lead(lead)  # must not raise

    updated = await db.get_lead(lead_id)
    assert updated["status"] == "failed"
    assert "crash" in updated["error"]
