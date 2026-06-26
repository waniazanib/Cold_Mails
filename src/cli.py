"""
cli.py
Typer entrypoint for the job discovery and outreach pipeline.

Commands:
    scout   - Phase 1: search DuckDuckGo for companies in a city/area
    enrich  - Phase 2: crawl contact pages for leads still missing an email
    leads   - list leads, optionally filtered by status (find an id for `track`)
    draft   - Phase 4: generate cold emails for leads marked Ready for Draft
    track   - Phase 5: manually record an outreach outcome (Sent/Replied/etc)
    stats   - Phase 6: funnel counts across every status

`leads` is not one of the phases in AppFlow.md, it's a small addition:
`track` needs a lead id, and the only other way to find one is opening
the SQLite Viewer in VS Code. This skips that detour for a quick lookup.

Only `scout` and `draft` touch Groq, the rest work with no GROQ_API_KEY
set at all, agents.py now builds its LLM and agents lazily for exactly
this reason.

Run from the project root: python src/cli.py <command> [options]

Dependencies: typer
"""

import logging
from typing import Optional

import typer

import crawler
import crew_copywriter
import crew_scout
import db

app = typer.Typer(help="Job discovery and outreach drafting pipeline.", no_args_is_help=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cli")

# Statuses track is allowed to set. Pending Email / Ready for Draft / Drafted
# are pipeline-internal, advanced automatically by scout/enrich/draft, not
# something to set by hand.
TRACK_STATUSES = {"Sent", "Replied", "No Response", "Interview"}

STATS_ORDER = [
    "Pending Email", "Ready for Draft", "Drafted",
    "Sent", "Replied", "No Response", "Interview", "Total",
]


# ---------------------------------------------------------------------------
# scout
# ---------------------------------------------------------------------------


@app.command()
def scout(
    city: str = typer.Option(..., prompt="City", help="City to search in."),
    area: str = typer.Option(
        "", prompt="Area (optional, press Enter to skip)", help="Area within the city."
    ),
    max_results: int = typer.Option(10, help="Max search results to request per query."),
    no_agent_summary: bool = typer.Option(
        False,
        "--no-agent-summary",
        help="Skip the agent's narrative summary (faster, makes no Groq call).",
    ),
):
    """Phase 1: search for companies and save new leads to leads.db."""
    area_value = area.strip() or None
    result = crew_scout.run_scout(
        city,
        area_value,
        max_results=max_results,
        use_agent_summary=not no_agent_summary,
    )

    typer.echo("\n--- Scout Run Summary ---")
    for key, value in result.items():
        if key == "agent_summary":
            continue
        typer.echo(f"{key}: {value}")
    if result.get("agent_summary"):
        typer.echo("\n--- Agent Summary ---")
        typer.echo(result["agent_summary"])


# ---------------------------------------------------------------------------
# enrich
# ---------------------------------------------------------------------------


@app.command()
def enrich():
    """Phase 2: crawl contact pages for every lead still missing an email."""
    conn = db.get_connection()
    db.init_db(conn)
    try:
        summary = crawler.enrich_pending_leads(conn)
    finally:
        conn.close()

    typer.echo("\n--- Enrich Run Summary ---")
    for key, value in summary.items():
        typer.echo(f"{key}: {value}")


# ---------------------------------------------------------------------------
# leads
# ---------------------------------------------------------------------------


@app.command()
def leads(
    status: Optional[str] = typer.Option(
        None, help=f"Filter by status. One of: {', '.join(sorted(db.STATUSES))}."
    ),
):
    """List leads, optionally filtered by status. Useful for finding an id before `track`."""
    conn = db.get_connection()
    db.init_db(conn)
    try:
        if status:
            if status not in db.STATUSES:
                typer.echo(f"Invalid status '{status}'. Must be one of: {', '.join(sorted(db.STATUSES))}")
                raise typer.Exit(code=1)
            rows = db.get_leads_by_status(conn, status)
        else:
            rows = conn.execute("SELECT * FROM leads ORDER BY date_added").fetchall()
    finally:
        conn.close()

    if not rows:
        typer.echo("No leads found.")
        return

    for row in rows:
        typer.echo(
            f"[{row['id']}] {row['company_name']} ({row['domain']}) "
            f"- {row['status']} - {row['contact_email'] or 'no email'}"
        )


# ---------------------------------------------------------------------------
# draft
# ---------------------------------------------------------------------------


@app.command()
def draft():
    """Phase 4: generate cold emails for every lead marked Ready for Draft."""
    summary = crew_copywriter.run_draft_batch()
    typer.echo("\n--- Draft Run Summary ---")
    for key, value in summary.items():
        typer.echo(f"{key}: {value}")


# ---------------------------------------------------------------------------
# track
# ---------------------------------------------------------------------------


@app.command()
def track(
    lead_id: int = typer.Argument(..., help="Lead id, see `leads` to find one."),
    status: str = typer.Argument(..., help=f"One of: {', '.join(sorted(TRACK_STATUSES))}."),
    notes: Optional[str] = typer.Option(None, help="Optional note to attach to this lead."),
):
    """Phase 5: manually record an outreach outcome after checking your own inbox."""
    if status not in TRACK_STATUSES:
        typer.echo(f"Invalid status '{status}'. Must be one of: {', '.join(sorted(TRACK_STATUSES))}")
        raise typer.Exit(code=1)

    conn = db.get_connection()
    db.init_db(conn)
    try:
        existing = db.get_lead_by_id(conn, lead_id)
        if existing is None:
            typer.echo(f"No lead found with id {lead_id}.")
            raise typer.Exit(code=1)
        db.update_status(conn, lead_id, status, notes=notes)
        company_name = existing["company_name"]
    finally:
        conn.close()

    typer.echo(f"Lead {lead_id} ({company_name}) marked '{status}'.")


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


@app.command()
def stats():
    """Phase 6: funnel counts across every status."""
    conn = db.get_connection()
    db.init_db(conn)
    try:
        counts = db.get_stats(conn)
    finally:
        conn.close()

    typer.echo("\n--- Funnel Stats ---")
    for key in STATS_ORDER:
        typer.echo(f"{key}: {counts.get(key, 0)}")


if __name__ == "__main__":
    app()