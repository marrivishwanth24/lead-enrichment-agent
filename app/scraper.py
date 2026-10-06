"""
scraper.py — fetch and clean a company's public website text for enrichment.

Deliberately the simplest thing that works: fetch the homepage, strip
script/style/nav/footer noise, return clean text. No paid enrichment API
(Clearbit, Apollo, etc.) required — this project should work out of the box
with only an Anthropic key. See README for the optional Tavily-based search
enrichment upgrade (broader signal than one page, needs its own API key).
"""

import httpx
from bs4 import BeautifulSoup

MAX_CHARS = 6000  # bound what gets sent to the LLM — a full page can be huge
_NOISE_TAGS = ["script", "style", "nav", "footer", "header", "noscript", "svg"]


class ScrapeError(RuntimeError):
    """Raised when a company's website can't be fetched or parsed."""


def _normalize_url(domain: str) -> str:
    domain = domain.strip()
    if not domain.startswith(("http://", "https://")):
        domain = f"https://{domain}"
    return domain


async def fetch_website_text(domain: str) -> str:
    """Fetch a domain's homepage and return cleaned, visible text."""
    url = _normalize_url(domain)
    try:
        async with httpx.AsyncClient(
            timeout=15.0, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; LeadEnrichmentBot/1.0)"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except httpx.HTTPError as e:
        raise ScrapeError(f"Could not fetch {url}: {e}") from e

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(_NOISE_TAGS):
        tag.decompose()

    text = soup.get_text(separator=" ", strip=True)
    text = " ".join(text.split())  # collapse repeated whitespace
    if not text:
        raise ScrapeError(f"{url} returned no readable text")

    return text[:MAX_CHARS]
