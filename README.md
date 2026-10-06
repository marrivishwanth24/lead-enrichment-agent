# Lead Enrichment Agent

Give it a company name and domain. It researches the company, decides
whether it fits your ICP, drafts a personalized outbound email if it does,
grades its own draft for fabricated claims and genericness, and queues it
for human approval — it never sends anything on its own.

A fully standalone project — no dependency on any other repo. Built around
the part of an agentic revenue-ops platform that isn't retrieval or
classification: **research, reasoning, and guarded generation.**

## What it does

1. **Enrich** — fetches the company's public website, has Claude synthesize
   a structured profile (what they do, likely industry, concrete signals).
2. **Qualify** — reasons about fit against a configurable ICP description,
   with an explicit yes/no and a stated reason, not a black box.
3. **Draft** — if qualified, generates a personalized outbound email that
   references real findings from enrichment, not a generic template.
4. **Grade** — a separate LLM call checks the draft against the enrichment
   profile: does every claim actually trace back to something real
   (`grounded`), and is it genuinely specific rather than boilerplate
   (`personalized`)? A draft that fails either check is flagged for closer
   review instead of silently landing in the approval queue.
5. **Queue** — qualified, graded leads land in a review queue. A human
   approves or rejects. **Nothing is ever auto-sent** — that's a structural
   guardrail, not a prompt instruction the model could ignore.

## Architecture

```
POST /leads {company_name, domain}
      │
      ▼
   SQLite (status: "pending")
      │
      ▼  background worker polls for pending leads
┌──────────────────────────────────────────────────────┐
│  LangGraph pipeline                                    │
│                                                          │
│  enrich --ok--> qualify --qualified--> draft --> grade  │
│    |               |                                     │
│  failed      disqualified                                │
│    |               |                                     │
│   END             END                              END   │
└──────────────────────────────────────────────────────┘
      │
      ▼
  status: pending_approval | flagged_for_review | disqualified | failed
      │
      ▼
  GET /drafts/pending  →  human reviews  →  POST /leads/{id}/approve|reject
```

Two real branch points: a scrape failure short-circuits cleanly instead of
crashing the worker, and a disqualified lead stops before a draft is ever
generated — no point drafting outreach to a lead that doesn't fit. The
grading outcome doesn't change which node runs (no conditional edge needed
there) — it changes what status the result gets afterward, which the worker
decides by reading the final state.

## Why SQLite + a polling worker, not Postgres + Redis/Celery?

This is a real, legitimate pattern — a status column plus a worker that
polls for rows in a given state is how plenty of production systems
implement a job queue before traffic volume justifies a dedicated broker —
not a toy stand-in. `db.claim_next_pending()` is the one place that
pattern lives; swapping it for a Redis/RQ or Celery-backed queue later
wouldn't touch `pipeline.py` at all, only how a lead gets picked up for
processing. For this project's scope, running the worker as an asyncio
background task inside the same process as the API (see `main.py`'s
lifespan handler) is the simplest thing that actually works; splitting it
into a dedicated worker process/deployment is a config change, not a
rewrite.

## Why a separate grading step, not just trust the draft?

An agent that takes an externally-visible action — even indirectly, by
putting a draft in front of a human who might not catch a fabricated claim
— deserves a guardrail that doesn't depend on the same model call getting
it right the first time. `grade_node` is an independent LLM call whose only
job is to try to catch what `draft_node` got wrong: claims not actually
in the enrichment profile, or a draft generic enough to have been written
about any company. If grading itself errors, the result fails closed
(`grounded=False`) rather than silently passing the draft through —
an unverified draft should never look identical to a verified one.

## Project Structure

```
lead-enrichment-agent/
├── app/
│   ├── main.py       ← FastAPI endpoints + lifespan-managed worker task
│   ├── pipeline.py    ← LangGraph state machine: enrich/qualify/draft/grade
│   ├── scraper.py      ← Fetch + clean a company website's text
│   ├── worker.py        ← Polls for pending leads, runs the pipeline, persists outcomes
│   └── db.py             ← SQLite schema + queries
├── tests/
│   ├── test_pipeline.py    ← Branch-point routing: scrape failure, disqualify, grading
│   ├── test_worker.py       ← Status resolution + lead processing
│   ├── test_db.py            ← Persistence layer
│   ├── test_scraper.py        ← Website fetching + cleaning (mocked)
│   └── test_main.py            ← API endpoints
├── requirements.txt
├── railway.json / Procfile
└── .env.example
```

## Getting Started

```bash
git clone https://github.com/marrivishwanth24/lead-enrichment-agent
cd lead-enrichment-agent
pip install -r requirements.txt
cp .env.example .env   # fill in ANTHROPIC_API_KEY
uvicorn app.main:app --reload
```

### Try it

```bash
curl -X POST localhost:8000/leads -H "Content-Type: application/json" \
  -d '{"company_name": "Stripe", "domain": "stripe.com"}'
# -> {"id": 1, "status": "pending"}

curl localhost:8000/leads/1
# poll this -- the background worker picks it up within ~2 seconds and
# advances it through enrich -> qualify -> draft -> grade

curl localhost:8000/drafts/pending
# once it reaches pending_approval, review it here

curl -X POST localhost:8000/leads/1/approve
```

### Customizing the ICP

`DEFAULT_ICP` in `app/pipeline.py` is a sensible default (small-to-mid B2B
software companies). Pass `icp_description` into `run_pipeline()` to
qualify against different criteria per call — a natural extension is
accepting it as an optional field on `POST /leads`.

## Running Tests

```bash
pip install -r requirements.txt
pytest
```

35 tests, all mocked at the network/LLM boundary — no API key or live
network access needed to run the suite. Covers the pipeline's branch
points (scrape failure, disqualification, the grading fail-closed
behavior), the worker's status-resolution logic, the persistence layer,
and every API endpoint.

## Roadmap

- **Broader enrichment signal**: the current scraper reads only the
  company's own homepage. A [Tavily](https://tavily.com)-backed search step
  (news, funding announcements, hiring signals) would widen the picture —
  deliberately left out of the MVP since it needs its own API key and the
  homepage-only version already demonstrates the real pattern.
- **Actual email sending**: approval currently just flips a status. Wiring
  a real send (e.g., via SendGrid) behind the `approved` status is a small,
  deliberate next step — kept separate so "draft something" and "send
  something" stay distinct, reviewable actions.
- **Redis/RQ or Celery** once a single polling worker can't keep up —
  `db.claim_next_pending()` is the only place that change touches.

## Author

**Vishwanth Marri**
- GitHub: [github.com/marrivishwanth24](https://github.com/marrivishwanth24)
