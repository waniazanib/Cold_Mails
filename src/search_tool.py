"""
search_tool.py
DuckDuckGo-backed company discovery tool for the Tech Scout agent.

Wraps the ddgs library (pip install ddgs, the current name for what used
to be duckduckgo_search) with retry/backoff and a fixed anti-bot delay,
filters out directory/aggregator sites, and extracts candidate domains
and snippet emails from raw search results.

Design note: the actual parsing/filtering/email-extraction logic lives
in plain functions (search_companies, extract_domain, etc.) that never
touch an LLM, so domains and emails are never put through a model that
could transcribe them wrong. The @tool-wrapped version exists so a
CrewAI agent can call this conversationally (e.g. to decide which
queries to try next), but crew_scout.py should call search_companies()
directly when the goal is reliably getting leads into the database.

Dependencies: ddgs, tenacity, langchain-core
Expected location: src/search_tool.py (imports db.py as a sibling module)
"""

import logging
import re
import time
from typing import Optional
from urllib.parse import urlparse

from ddgs import DDGS
from ddgs.exceptions import DDGSException
from langchain_core.tools import tool
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from db import EMAIL_RE, normalize_domain

logger = logging.getLogger("search_tool")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum pause before every search call, independent of retry backoff.
SEARCH_DELAY_SECONDS = 3.0

# Sites that show up constantly but are never a real company's own homepage.
# Includes directories, aggregators, media outlets, and social platforms.
DIRECTORY_DOMAINS = {
    # Job boards / company directories
    "yelp.com", "glassdoor.com", "indeed.com", "linkedin.com",
    "crunchbase.com", "clutch.co", "goodfirms.co", "trustpilot.com",
    "bbb.org", "yellowpages.com", "rocketreach.co", "apollographql.com",
    "zoominfo.com", "g2.com", "capterra.com", "softwareadvice.com",
    "expertise.com", "upcity.com", "designrush.com", "bark.com",
    "toptal.com", "upwork.com", "freelancer.com", "fiverr.com",
    # Social / community
    "facebook.com", "instagram.com", "twitter.com", "x.com",
    "youtube.com", "tiktok.com", "reddit.com", "quora.com",
    "pinterest.com", "snapchat.com", "telegram.org",
    # Media / news / blog platforms
    "medium.com", "substack.com", "wordpress.com", "blogger.com",
    "cnbc.com", "forbes.com", "inc.com", "entrepreneur.com",
    "techcrunch.com", "wired.com", "businessinsider.com",
    "thenextweb.com", "venturebeat.com", "zdnet.com", "cnet.com",
    "pcmag.com", "towardsdatascience.com", "hackernoon.com",
    "dev.to", "hashnode.dev",
    # General web
    "wikipedia.org", "wikimedia.org", "google.com", "bing.com",
    "duckduckgo.com", "yahoo.com", "github.com", "stackoverflow.com",
    "aws.amazon.com", "amazon.com", "microsoft.com", "apple.com",
    # Pakistan-specific local directories
    "rozee.pk", "mustakbil.com", "paklaps.com", "olx.com.pk",
}

# Title patterns that signal a listicle blog post rather than a company.
# A result whose title matches any of these is dropped entirely,
# regardless of its domain.
LISTICLE_PATTERNS = re.compile(
    r"(top\s+\d+|best\s+\d+|\d+\s+best|\d+\s+top|"
    r"top\s+web|best\s+software|leading\s+companies|"
    r"list\s+of|companies\s+in\s+pakistan|"
    r"fintech\s+companies|startup\s+ecosystem)",
    re.IGNORECASE,
)

# Loose pattern for finding email-looking substrings inside snippet text.
SNIPPET_EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")


# ---------------------------------------------------------------------------
# Query building
# ---------------------------------------------------------------------------


def build_search_query(city: str, area: Optional[str] = None) -> str:
    """
    Build a search string targeting actual company homepages, not listicle
    blog posts. The quoted phrase and negative terms push DDG toward real
    company sites and away from 'Top 10 software houses' roundups.
    """
    city = city.strip()
    location = f"{area.strip()}, {city}" if area and area.strip() else city
    return (
        f'"software house" OR "IT company" {location} '
        f'-site:clutch.co -site:goodfirms.co -site:linkedin.com '
        f'-site:facebook.com -site:tiktok.com '
        f'-"top 10" -"top 5" -"best software houses" -"list of"'
    )


# ---------------------------------------------------------------------------
# Domain / directory filtering
# ---------------------------------------------------------------------------


def extract_domain(url: str) -> Optional[str]:
    """Pull a normalized root domain out of a search result URL."""
    if not url:
        return None
    try:
        netloc = urlparse(url).netloc
    except ValueError:
        return None
    if not netloc:
        return None
    return normalize_domain(netloc)


def is_directory_domain(domain: str) -> bool:
    """True if the domain is a known aggregator/directory/media site."""
    return domain in DIRECTORY_DOMAINS


def is_listicle_title(title: str) -> bool:
    """
    True if the result title reads like a blog listicle rather than a
    company's own page. Catches results like 'Top 10 Software Houses in
    Faisalabad' or 'Best Web Development Companies in Pakistan'.
    """
    return bool(LISTICLE_PATTERNS.search(title))


# ---------------------------------------------------------------------------
# Email extraction
# ---------------------------------------------------------------------------


def extract_email_from_text(text: str) -> Optional[str]:
    """
    Pull the first strictly-valid email out of snippet text. Returns
    None if nothing in the text passes db.EMAIL_RE. Most snippets will
    not contain one, that's expected, see AppFlow.md Phase 2 (enrich)
    and Phase 3 (manual override) for the fallback paths.
    """
    if not text:
        return None
    for candidate in SNIPPET_EMAIL_PATTERN.findall(text):
        if EMAIL_RE.match(candidate):
            return candidate
    return None


# ---------------------------------------------------------------------------
# Raw search call, retried and rate-limited
# ---------------------------------------------------------------------------


@retry(
    retry=retry_if_exception_type(DDGSException),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=3, min=3, max=30),
    reraise=True,
)
def _raw_search(query: str, max_results: int = 10) -> list[dict]:
    """
    Single retried DuckDuckGo call. Always pauses SEARCH_DELAY_SECONDS
    first, on top of tenacity's exponential backoff between retries, so
    even the very first attempt of every query is rate-limit-polite.
    """
    time.sleep(SEARCH_DELAY_SECONDS)
    return DDGS().text(query, max_results=max_results)


# ---------------------------------------------------------------------------
# Public search function (plain, testable, no LangChain involved)
# ---------------------------------------------------------------------------


def search_companies(query: str, max_results: int = 10) -> list[dict]:
    """
    Run a discovery search and return cleaned candidate leads.

    Each item:
        {
            "company_name": str,        # cleaned from the result title
            "raw_url": str,
            "domain": str,
            "snippet_email": str | None,
            "source_query": str,
        }

    Directory/aggregator results and results with no usable domain are
    dropped. Duplicate domains within the same result page are dropped
    too. This function never touches the database, callers (e.g.
    crew_scout.py) decide what to do with the leads, including the
    fuzzy/exact dedup check that lives in db.py against existing rows.

    On a search failure after all retries, logs the error and returns
    an empty list rather than raising, so one bad query doesn't kill
    a whole scout run.
    """
    try:
        raw_results = _raw_search(query, max_results=max_results)
    except DDGSException as exc:
        logger.error("Search failed after retries for query '%s': %s", query, exc)
        return []

    leads = []
    seen_domains = set()

    for result in raw_results:
        url = result.get("href", "")
        title = (result.get("title") or "").strip()
        body = result.get("body", "")

        domain = extract_domain(url)
        if not domain:
            continue
        if is_directory_domain(domain):
            logger.debug("Skipping directory domain: %s", domain)
            continue
        if is_listicle_title(title):
            logger.debug("Skipping listicle result: %s", title)
            continue
        if domain in seen_domains:
            continue
        seen_domains.add(domain)

        leads.append({
            "company_name": title or domain,
            "raw_url": url,
            "domain": domain,
            "snippet_email": extract_email_from_text(f"{title} {body}"),
            "source_query": query,
        })

    logger.info(
        "Query '%s' returned %d candidate leads (%d raw results)",
        query, len(leads), len(raw_results),
    )
    return leads


# ---------------------------------------------------------------------------
# LangChain tool wrapper, for the CrewAI Tech Scout agent
# ---------------------------------------------------------------------------


@tool
def search_tech_companies(query: str) -> str:
    """
    Search DuckDuckGo for technology companies matching the query and
    return a list of candidate leads (company name, domain, and any
    email found in the snippet). Directory sites like LinkedIn or
    Glassdoor are filtered out automatically. Use a query shaped like
    'software houses tech companies in <area>, <city>'.
    """
    leads = search_companies(query)
    if not leads:
        return "No candidate companies found for this query."

    lines = []
    for lead in leads:
        email_part = f", email: {lead['snippet_email']}" if lead["snippet_email"] else ""
        lines.append(f"- {lead['company_name']} ({lead['domain']}){email_part}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Manual smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    test_query = build_search_query("Lahore", "Johar Town")
    print(f"Query: {test_query}")
    for lead in search_companies(test_query, max_results=5):
        print(lead)