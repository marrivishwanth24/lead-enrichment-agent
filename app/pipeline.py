"""
pipeline.py — the enrichment/qualification/drafting LangGraph state machine.

    enrich --ok--> qualify --qualified--> draft --> grade --> END
      |               |
    failed      disqualified
      |               |
     END             END

Two real branch points: a scrape failure short-circuits cleanly instead of
crashing the worker, and a disqualified lead stops before a draft is ever
generated (no point drafting outreach to a lead that doesn't fit). grade's
output (grounded/personalized) doesn't change which node runs next, just
what status the caller assigns afterward — no conditional edge needed there,
only where the graph actually takes a different path.

All four LLM steps use with_structured_output (Pydantic) rather than parsed
free text, same pattern used in the RAG platform's orchestrator — a
malformed response can't silently corrupt a lead's record.
"""

from typing import TypedDict

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from app.scraper import ScrapeError, fetch_website_text

_haiku = ChatAnthropic(model="claude-haiku-4-5-20251001", max_tokens=400)
_sonnet = ChatAnthropic(model="claude-sonnet-4-6", max_tokens=500)

DEFAULT_ICP = (
    "Small to mid-size B2B software companies (10-200 employees) that would "
    "benefit from AI-powered workflow automation. Good fit: clear product "
    "offering, active online presence, signs of growth. Poor fit: personal "
    "blogs, non-business sites, companies with no apparent commercial product."
)


# ── Structured-output schemas ────────────────────────────────────────────────

class EnrichmentSummary(BaseModel):
    what_they_do: str = Field(description="1-2 sentence summary of the company's product/service")
    likely_industry: str = Field(description="Best-guess industry/category")
    signals: list[str] = Field(description="2-4 short, concrete facts or buying signals found on the site")


class Qualification(BaseModel):
    qualified: bool = Field(description="True if this lead fits the ICP description")
    reason: str = Field(description="One sentence explaining the judgment")


class EmailDraft(BaseModel):
    subject: str = Field(description="A short, specific email subject line, not generic")
    body: str = Field(description="A brief, personalized outbound email body, 3-5 sentences")


class DraftGrade(BaseModel):
    grounded: bool = Field(description="True only if every factual claim in the draft traces to the enrichment summary")
    personalized: bool = Field(description="True if the draft references specifics, not generic boilerplate that could apply to any company")
    notes: str = Field(description="One sentence explaining the judgment")


_enricher = _haiku.with_structured_output(EnrichmentSummary)
_qualifier = _haiku.with_structured_output(Qualification)
_drafter = _sonnet.with_structured_output(EmailDraft)
_grader = _haiku.with_structured_output(DraftGrade)


# ── State ─────────────────────────────────────────────────────────────────────

class LeadState(TypedDict, total=False):
    company_name: str
    domain: str
    icp_description: str
    raw_website_text: str
    enrichment_summary: dict
    qualified: bool
    qualification_reason: str
    draft_subject: str
    draft_body: str
    grading_grounded: bool
    grading_personalized: bool
    grading_notes: str
    error: str


# ── Nodes ─────────────────────────────────────────────────────────────────────

async def enrich_node(state: LeadState) -> dict:
    try:
        text = await fetch_website_text(state["domain"])
    except ScrapeError as e:
        return {"error": str(e)}

    try:
        summary = await _enricher.ainvoke([
            HumanMessage(content=(
                f"Summarize this company's website for a sales research profile.\n\n"
                f"Company: {state['company_name']}\n\nWebsite text:\n{text}"
            ))
        ])
    except Exception as e:
        return {"error": f"Enrichment synthesis failed: {e}"}

    return {
        "raw_website_text": text,
        "enrichment_summary": summary.model_dump(),
    }


def route_after_enrich(state: LeadState) -> str:
    return "failed" if state.get("error") else "ok"


async def qualify_node(state: LeadState) -> dict:
    icp = state.get("icp_description") or DEFAULT_ICP
    summary = state["enrichment_summary"]
    try:
        result = await _qualifier.ainvoke([
            HumanMessage(content=(
                f"ICP criteria:\n{icp}\n\n"
                f"Company profile:\n{summary}\n\n"
                f"Does this company fit the ICP?"
            ))
        ])
    except Exception as e:
        return {"error": f"Qualification failed: {e}"}

    return {"qualified": result.qualified, "qualification_reason": result.reason}


def route_after_qualify(state: LeadState) -> str:
    if state.get("error"):
        return "disqualified"  # fail closed: don't draft outreach on an error
    return "qualified" if state.get("qualified") else "disqualified"


async def draft_node(state: LeadState) -> dict:
    summary = state["enrichment_summary"]
    try:
        draft = await _drafter.ainvoke([
            HumanMessage(content=(
                f"Write a short, personalized outbound email to {state['company_name']}.\n\n"
                f"What they do: {summary.get('what_they_do')}\n"
                f"Signals: {', '.join(summary.get('signals', []))}\n"
                f"Why they're a fit: {state.get('qualification_reason', '')}\n\n"
                "Reference at least one specific, real detail from their profile. "
                "No generic filler sentences that could apply to any company."
            ))
        ])
    except Exception as e:
        return {"error": f"Drafting failed: {e}"}

    return {"draft_subject": draft.subject, "draft_body": draft.body}


async def grade_node(state: LeadState) -> dict:
    try:
        grade = await _grader.ainvoke([
            HumanMessage(content=(
                f"Company profile (ground truth):\n{state['enrichment_summary']}\n\n"
                f"Draft email:\nSubject: {state['draft_subject']}\n{state['draft_body']}\n\n"
                "Judge whether the draft's claims are grounded in the profile above, "
                "and whether it's genuinely personalized rather than generic."
            ))
        ])
    except Exception:
        # Fail closed: if grading itself errors, treat the draft as unverified
        # rather than silently letting it through to the approval queue.
        return {"grading_grounded": False, "grading_personalized": False, "grading_notes": "Grading step errored"}

    return {
        "grading_grounded": grade.grounded,
        "grading_personalized": grade.personalized,
        "grading_notes": grade.notes,
    }


def _build_graph():
    g = StateGraph(LeadState)
    g.add_node("enrich", enrich_node)
    g.add_node("qualify", qualify_node)
    g.add_node("draft", draft_node)
    g.add_node("grade", grade_node)

    g.set_entry_point("enrich")
    g.add_conditional_edges("enrich", route_after_enrich, {"ok": "qualify", "failed": END})
    g.add_conditional_edges("qualify", route_after_qualify, {"qualified": "draft", "disqualified": END})
    g.add_edge("draft", "grade")
    g.add_edge("grade", END)
    return g.compile()


_graph = _build_graph()


async def run_pipeline(company_name: str, domain: str, icp_description: str | None = None) -> LeadState:
    """Run a lead through the full enrich -> qualify -> draft -> grade pipeline."""
    result = await _graph.ainvoke({
        "company_name": company_name,
        "domain": domain,
        "icp_description": icp_description or DEFAULT_ICP,
    })
    return result
