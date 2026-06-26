"""
crew_scout.py
Phase 1 orchestration: Interactive Target Discovery.

Prompts for City (required) and Area (optional), searches, and writes
leads into leads.db. Single-track decision in effect, every lead is
inserted with category="AI Engineer", see agents.py for the note on
why the Python Developer track was dropped.

Design note on how the CrewAI agent is actually used here: the real
data path is search_tool.search_companies() called directly, plain
structured results in, plain db.insert_lead() out, no LLM involved.
Calling tech_scout's own tool-driven task here as well would issue a
second DuckDuckGo query for the same search, doubling the rate-limit
exposure for zero benefit. Instead, the agent is handed the leads that
were already found and asked to write a short summary of the batch,
real CrewAI/Groq usage, but reasoning over verified data instead of
re-deriving it and risking a transcription error on a domain or email.
Set use_agent_summary=False to skip this and run fully deterministic.

Dependencies: crewai (already required by agents.py)
Expected location: src/crew_scout.py
"""

import logging
import sqlite3
from typing import Optional

from crewai import Crew, Task

import db
from agents import get_tech_scout
from search_tool import build_search_query, search_companies

logger = logging.getLogger("crew_scout")


# ---------------------------------------------------------------------------
# Agent summary (optional, reasons over already-found leads, no re-search)
# ---------------------------------------------------------------------------


def build_summary_task(leads: list[dict], location: str) -> Task:
    """
    Summarize leads that were already found by search_companies().
    The list is handed to the agent inline, it is explicitly told not
    to add, remove, or invent any company, this is a reasoning/writing
    task, not a research task.
    """
    if leads:
        leads_block = "\n".join(
            f"- {lead['company_name']} ({lead['domain']})"
            + (
                f", email found: {lead['snippet_email']}"
                if lead["snippet_email"]
                else ", no email found"
            )
            for lead in leads
        )
    else:
        leads_block = "(none found)"

    return Task(
        description=(
            f"Here are the technology companies already found by search in "
            f"{location}. This list is already verified, do not add, "
            f"remove, or invent any company not on it:\n\n{leads_block}\n\n"
            f"Write a short summary (3-5 sentences) of this batch for the "
            f"job seeker: how many companies were found, how many already "
            f"have a usable contact email versus needing manual lookup, "
            f"and one observation about the batch."
        ),
        expected_output="A short 3-5 sentence summary of the batch.",
        agent=get_tech_scout(),
    )


def summarize_with_agent(leads: list[dict], city: str, area: Optional[str] = None) -> str:
    """Run the tech_scout agent over already-found leads. Makes one Groq call."""
    location = f"{area}, {city}" if area else city
    task = build_summary_task(leads, location)
    crew = Crew(agents=[get_tech_scout()], tasks=[task], verbose=False)
    result = crew.kickoff()
    return str(result)


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


def run_scout(
    city: str,
    area: Optional[str] = None,
    category: str = "AI Engineer",
    max_results: int = 10,
    conn: Optional[sqlite3.Connection] = None,
    use_agent_summary: bool = True,
) -> dict:
    """
    Run one full scout pass: search, then insert results into leads.db.

    If conn is None, opens and owns a real on-disk connection via
    db.get_connection() / db.DEFAULT_DB_PATH, and closes it before
    returning. Pass an in-memory connection explicitly for testing
    without touching the real database file.

    Returns a summary dict with counts plus an optional agent_summary
    string. A failure in the agent summary step is logged and leaves
    agent_summary as None, it never causes already-inserted leads to
    be lost or the function to raise.
    """
    owns_connection = conn is None
    if conn is None:
        conn = db.get_connection()
        db.init_db(conn)

    try:
        query = build_search_query(city, area)
        logger.info("Scout query: %s", query)

        leads = search_companies(query, max_results=max_results)

        inserted = 0
        skipped_duplicate = 0
        with_email = 0

        for lead in leads:
            email = lead["snippet_email"]
            new_id = db.insert_lead(
                conn,
                company_name=lead["company_name"],
                raw_domain=lead["domain"],
                category=category,
                processing_mode="Automatic",
                contact_email=email,
                email_source="ddg_snippet" if email else None,
                source_query=lead["source_query"],
            )
            if new_id is None:
                skipped_duplicate += 1
            else:
                inserted += 1
                if email:
                    with_email += 1

        summary = {
            "query": query,
            "raw_candidates": len(leads),
            "inserted": inserted,
            "skipped_duplicate": skipped_duplicate,
            "with_email": with_email,
            "pending_email": inserted - with_email,
            "agent_summary": None,
        }
        logger.info("Scout run complete: %s", summary)

        if use_agent_summary and leads:
            try:
                summary["agent_summary"] = summarize_with_agent(leads, city, area)
            except Exception as exc:  # noqa: BLE001 - any LLM/network failure here is non-fatal
                logger.warning("Agent summary failed, continuing without it: %s", exc)

        return summary
    finally:
        if owns_connection:
            conn.close()


# ---------------------------------------------------------------------------
# Interactive entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    city_input = input("City (required): ").strip()
    while not city_input:
        city_input = input("City cannot be empty, try again: ").strip()
    area_input = input("Area (optional, press Enter to skip): ").strip() or None

    result = run_scout(city_input, area_input)

    print("\n--- Scout Run Summary ---")
    for key, value in result.items():
        if key == "agent_summary":
            continue
        print(f"{key}: {value}")

    if result.get("agent_summary"):
        print("\n--- Agent Summary ---")
        print(result["agent_summary"])