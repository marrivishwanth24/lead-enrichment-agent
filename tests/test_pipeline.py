"""Tests for the LangGraph pipeline's routing: the scrape-failure and
qualify/disqualify branch points, and the grading outcome. Mocks the LLM
calls and the scraper -- no real network or API calls."""

from unittest.mock import AsyncMock, patch

import pytest

from app.pipeline import (
    DraftGrade,
    EmailDraft,
    EnrichmentSummary,
    Qualification,
    run_pipeline,
)
from app.scraper import ScrapeError

SUMMARY = EnrichmentSummary(
    what_they_do="Builds developer tools",
    likely_industry="Software",
    signals=["Raised Series A", "Hiring engineers"],
)


@pytest.mark.asyncio
async def test_happy_path_qualified_grounded_draft():
    with patch("app.pipeline.fetch_website_text", new=AsyncMock(return_value="site text")), \
         patch("app.pipeline._enricher") as mock_enricher, \
         patch("app.pipeline._qualifier") as mock_qualifier, \
         patch("app.pipeline._drafter") as mock_drafter, \
         patch("app.pipeline._grader") as mock_grader:
        mock_enricher.ainvoke = AsyncMock(return_value=SUMMARY)
        mock_qualifier.ainvoke = AsyncMock(return_value=Qualification(qualified=True, reason="Good fit"))
        mock_drafter.ainvoke = AsyncMock(return_value=EmailDraft(subject="Hi", body="Saw you raised a Series A..."))
        mock_grader.ainvoke = AsyncMock(return_value=DraftGrade(grounded=True, personalized=True, notes="ok"))

        result = await run_pipeline("Acme", "acme.com")

    assert result["qualified"] is True
    assert result["draft_subject"] == "Hi"
    assert result["grading_grounded"] is True
    assert result["grading_personalized"] is True
    assert "error" not in result or not result["error"]


@pytest.mark.asyncio
async def test_scrape_failure_short_circuits_before_any_llm_call():
    with patch("app.pipeline.fetch_website_text", new=AsyncMock(side_effect=ScrapeError("unreachable"))), \
         patch("app.pipeline._enricher") as mock_enricher, \
         patch("app.pipeline._qualifier") as mock_qualifier:
        mock_enricher.ainvoke = AsyncMock()
        mock_qualifier.ainvoke = AsyncMock()

        result = await run_pipeline("Acme", "acme.com")

    assert result["error"]
    assert "qualified" not in result
    mock_enricher.ainvoke.assert_not_called()
    mock_qualifier.ainvoke.assert_not_called()


@pytest.mark.asyncio
async def test_disqualified_lead_never_reaches_drafting():
    with patch("app.pipeline.fetch_website_text", new=AsyncMock(return_value="site text")), \
         patch("app.pipeline._enricher") as mock_enricher, \
         patch("app.pipeline._qualifier") as mock_qualifier, \
         patch("app.pipeline._drafter") as mock_drafter:
        mock_enricher.ainvoke = AsyncMock(return_value=SUMMARY)
        mock_qualifier.ainvoke = AsyncMock(return_value=Qualification(qualified=False, reason="Not a fit"))
        mock_drafter.ainvoke = AsyncMock()

        result = await run_pipeline("Acme", "acme.com")

    assert result["qualified"] is False
    assert "draft_subject" not in result
    mock_drafter.ainvoke.assert_not_called()


@pytest.mark.asyncio
async def test_ungrounded_draft_is_reflected_in_state_not_silently_approved():
    with patch("app.pipeline.fetch_website_text", new=AsyncMock(return_value="site text")), \
         patch("app.pipeline._enricher") as mock_enricher, \
         patch("app.pipeline._qualifier") as mock_qualifier, \
         patch("app.pipeline._drafter") as mock_drafter, \
         patch("app.pipeline._grader") as mock_grader:
        mock_enricher.ainvoke = AsyncMock(return_value=SUMMARY)
        mock_qualifier.ainvoke = AsyncMock(return_value=Qualification(qualified=True, reason="Good fit"))
        mock_drafter.ainvoke = AsyncMock(return_value=EmailDraft(subject="Hi", body="Generic pitch"))
        mock_grader.ainvoke = AsyncMock(return_value=DraftGrade(grounded=False, personalized=False, notes="fabricated claim"))

        result = await run_pipeline("Acme", "acme.com")

    # The draft still exists -- grading doesn't erase it -- but the grade
    # fields make clear it shouldn't go straight to the approval queue.
    assert result["draft_subject"] == "Hi"
    assert result["grading_grounded"] is False


@pytest.mark.asyncio
async def test_grading_error_fails_closed_not_open():
    with patch("app.pipeline.fetch_website_text", new=AsyncMock(return_value="site text")), \
         patch("app.pipeline._enricher") as mock_enricher, \
         patch("app.pipeline._qualifier") as mock_qualifier, \
         patch("app.pipeline._drafter") as mock_drafter, \
         patch("app.pipeline._grader") as mock_grader:
        mock_enricher.ainvoke = AsyncMock(return_value=SUMMARY)
        mock_qualifier.ainvoke = AsyncMock(return_value=Qualification(qualified=True, reason="Good fit"))
        mock_drafter.ainvoke = AsyncMock(return_value=EmailDraft(subject="Hi", body="body"))
        mock_grader.ainvoke = AsyncMock(side_effect=RuntimeError("API down"))

        result = await run_pipeline("Acme", "acme.com")

    # A grading failure must never be treated as "passed" -- that would let
    # an unverified draft reach a human as if it had been checked.
    assert result["grading_grounded"] is False
