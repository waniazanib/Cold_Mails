"""
crawler.py
Contact-page crawler for the 'enrich' phase, automated email lookup.

For each domain still missing a contact email after Scout, tries a
small fixed set of likely pages (home, contact, about, team, careers),
parses each with BeautifulSoup, and regex-extracts a role-based email
address if one is present. Plain requests + BeautifulSoup, no crawling
framework, this is a narrow same-domain lookup, not a deep multi-site
crawl.

Politeness built in: a robots.txt check before every path, a realistic
User-Agent, a per-request timeout, and a fixed delay between domains.

Design note: like search_tool.py, the email extraction and validation
here is deterministic Python (db.EMAIL_RE), no LLM involved. The
crawler decides what email to attach to a lead, the agent layer never
gets a chance to transcribe it wrong.

Dependencies: requests, beautifulsoup4
Expected location: src/crawler.py (imports db.py as a sibling module)
"""

import logging
import re
import time
from typing import Optional
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from db import EMAIL_RE, get_leads_by_status, update_email

logger = logging.getLogger("crawler")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CANDIDATE_PATHS = [
    "/",
    "/contact",
    "/contact-us",
    "/about",
    "/about-us",
    "/team",
    "/careers",
]

REQUEST_TIMEOUT_SECONDS = 8
CRAWL_DELAY_SECONDS = 2.0

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT}

# Loose pattern for finding email-looking substrings inside page text,
# candidates get re-validated with the strict db.EMAIL_RE before use.
PAGE_EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

# Checked in this order: role-based addresses first, then generic ones.
ROLE_PREFIXES = ("hr", "careers", "jobs", "recruiting", "talent")
GENERIC_PREFIXES = ("info", "contact", "hello", "team")
# Never picked unless it's the only address found on the whole domain.
EXCLUDED_PREFIXES = ("noreply", "no-reply", "support", "billing", "admin", "webmaster")

# robots.txt parsers, cached per domain for the life of one enrich run.
_robots_cache: dict[str, Optional[RobotFileParser]] = {}


# ---------------------------------------------------------------------------
# robots.txt etiquette
# ---------------------------------------------------------------------------


def _get_robot_parser(domain: str) -> Optional[RobotFileParser]:
    """
    Fetch and parse robots.txt for a domain, cached per domain. Returns
    None (meaning "allow everything") if robots.txt is missing or
    unreachable, this is a handful of public marketing pages, not a
    scraping operation, absence of a robots.txt shouldn't block it.
    """
    if domain in _robots_cache:
        return _robots_cache[domain]

    parser: Optional[RobotFileParser] = None
    try:
        response = requests.get(
            f"https://{domain}/robots.txt", headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS
        )
        if response.status_code == 200:
            parser = RobotFileParser()
            parser.parse(response.text.splitlines())
    except requests.exceptions.RequestException as exc:
        logger.debug("Could not fetch robots.txt for %s: %s", domain, exc)

    _robots_cache[domain] = parser
    return parser


def is_path_allowed(domain: str, path: str) -> bool:
    """True if robots.txt (or its absence) permits fetching this path."""
    parser = _get_robot_parser(domain)
    if parser is None:
        return True
    return parser.can_fetch(USER_AGENT, path)


# ---------------------------------------------------------------------------
# Page fetch, isolated for easy mocking in tests
# ---------------------------------------------------------------------------


def fetch_page(url: str) -> Optional[str]:
    """
    Fetch a single page's HTML text. Returns None on any failure
    (connection error, timeout, SSL error, non-2xx status) rather than
    raising, callers move on to the next candidate path or domain.
    """
    try:
        response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
        if response.status_code != 200:
            logger.debug("Non-200 status %d for %s", response.status_code, url)
            return None
        return response.text
    except requests.exceptions.SSLError as exc:
        logger.debug("SSL error fetching %s: %s", url, exc)
    except requests.exceptions.Timeout as exc:
        logger.debug("Timeout fetching %s: %s", url, exc)
    except requests.exceptions.ConnectionError as exc:
        logger.debug("Connection error fetching %s: %s", url, exc)
    except requests.exceptions.RequestException as exc:
        logger.debug("Request error fetching %s: %s", url, exc)
    return None


# ---------------------------------------------------------------------------
# Email extraction / prioritization
# ---------------------------------------------------------------------------


def extract_emails(html: str) -> list[str]:
    """
    Pull every strictly-valid, deduped email out of a page, both from
    visible text and from mailto: links (which sometimes carry an
    address that isn't rendered as plain text on the page).
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator=" ")

    mailto_candidates = [
        a["href"].replace("mailto:", "").split("?")[0]
        for a in soup.find_all("a", href=True)
        if a["href"].lower().startswith("mailto:")
    ]

    candidates = PAGE_EMAIL_PATTERN.findall(text) + mailto_candidates
    valid = {c.strip().lower() for c in candidates if EMAIL_RE.match(c.strip())}
    return sorted(valid)


def pick_best_email(emails: list[str]) -> Optional[str]:
    """
    Prioritize role-based addresses (hr@, careers@, ...) over generic
    ones (info@, contact@, ...), and avoid clearly non-human addresses
    (noreply@, support@) unless nothing else was found at all.
    """
    if not emails:
        return None

    def prefix(email: str) -> str:
        return email.split("@")[0]

    role_matches = [e for e in emails if prefix(e) in ROLE_PREFIXES]
    if role_matches:
        return role_matches[0]

    generic_matches = [e for e in emails if prefix(e) in GENERIC_PREFIXES]
    if generic_matches:
        return generic_matches[0]

    non_excluded = [e for e in emails if prefix(e) not in EXCLUDED_PREFIXES]
    if non_excluded:
        return non_excluded[0]

    return emails[0]


# ---------------------------------------------------------------------------
# Single-domain crawl
# ---------------------------------------------------------------------------


def crawl_domain(domain: str) -> Optional[str]:
    """
    Try each candidate path against a domain in order, skipping any
    path robots.txt disallows, stop at the first page that yields a
    usable email. Returns None if nothing was found after exhausting
    all candidate paths, the lead stays 'Pending Email' for manual
    lookup (AppFlow.md Phase 3).

    Known limitation: HTTPS only. A site with no valid HTTPS will fail
    every path and fall straight through to manual lookup rather than
    retrying over plain HTTP.
    """
    base_url = f"https://{domain}"
    for path in CANDIDATE_PATHS:
        if not is_path_allowed(domain, path):
            logger.debug("robots.txt disallows %s%s, skipping", domain, path)
            continue

        url = urljoin(base_url, path)
        html = fetch_page(url)
        if html is None:
            continue

        best = pick_best_email(extract_emails(html))
        if best:
            logger.info("Found email for %s on %s: %s", domain, path, best)
            return best

    logger.info("No email found for %s after trying %d paths", domain, len(CANDIDATE_PATHS))
    return None


# ---------------------------------------------------------------------------
# Batch enrichment over all Pending Email leads
# ---------------------------------------------------------------------------


def enrich_pending_leads(conn) -> dict:
    """
    Crawl every 'Pending Email' lead in the database. Attaches an email
    and flips status to 'Ready for Draft' via db.update_email on a hit,
    leaves the row untouched on a miss. Returns a summary dict for the
    'enrich' CLI command to print.
    """
    pending = get_leads_by_status(conn, "Pending Email")
    found = 0
    missed = 0

    for row in pending:
        email = crawl_domain(row["domain"])
        if email:
            update_email(conn, row["id"], email, source="crawler")
            found += 1
        else:
            missed += 1
        time.sleep(CRAWL_DELAY_SECONDS)

    summary = {"attempted": len(pending), "found": found, "missed": missed}
    logger.info("Enrich run complete: %s", summary)
    return summary


# ---------------------------------------------------------------------------
# Manual smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    test_domain = "example.com"
    result = crawl_domain(test_domain)
    print(f"{test_domain} -> {result}")