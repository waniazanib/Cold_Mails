"""
crew_copywriter.py
Phase 4 orchestration: Segmented Copy Generation.

Pulls every 'Ready for Draft' lead, runs the outreach_copywriter agent
(from agents.py) to write a cold email, appends it to
output/cold_email_drafts.md, and flips status to 'Drafted'.

Single-track decision in effect: build_draft_task() in agents.py always
loads resume_ai.txt, there is no category branching here anymore.

A per-lead failure (Groq error, rate limit, anything) is logged and
that lead is left at 'Ready for Draft' for a retry on the next run,
rather than crashing the whole batch or silently marking it Drafted
with no actual content.

Dependencies: crewai (already required by agents.py)
Expected location: src/crew_copywriter.py
"""

import logging
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from crewai import Crew

import db
from agents import build_draft_task, get_outreach_copywriter

logger = logging.getLogger("crew_copywriter")

DEFAULT_OUTPUT_PATH = Path(__file__).resolve().parent.parent / "output" / "cold_email_drafts.md"

# Pause between Groq calls. Conservative default, free-tier rate limits
# vary by account, raise this if you start seeing rate-limit errors in
# the failed count.
DRAFT_DELAY_SECONDS = 2.0

# Word count past which a draft gets flagged in the logs, not blocked,
# just a visibility signal that something ran long against the
# under-150-word target in the task prompt.
WORD_COUNT_WARNING_THRESHOLD = 170

EM_DASH_PATTERN = re.compile(r"\s*[—–]\s*")


# ---------------------------------------------------------------------------
# Output sanitization
# ---------------------------------------------------------------------------


def sanitize_draft(text: str) -> str:
    """
    Deterministic safety net for the no-em-dash rule. The task prompt
    in agents.py already instructs the model not to use one, but a
    style instruction inside a prompt isn't a guarantee, this makes it
    one regardless of what the model actually outputs.
    """
    text = EM_DASH_PATTERN.sub(", ", text)
    text = re.sub(r",\s*,", ",", text)  # collapse a double comma the replace can create
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Output file
# ---------------------------------------------------------------------------


def append_draft_to_file(
    path: Path,
    company_name: str,
    domain: str,
    email: Optional[str],
    body: str,
) -> None:
    """Append one formatted draft block to cold_email_drafts.md."""
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    block = (
        f"## {company_name} ({domain})\n"
        f"**To:** {email or 'unknown'}\n"
        f"**Drafted:** {timestamp}\n\n"
        f"{body.strip()}\n\n"
        f"---\n\n"
    )
    with open(path, "a", encoding="utf-8") as f:
        f.write(block)


# ---------------------------------------------------------------------------
# Single draft
# ---------------------------------------------------------------------------


def draft_one_email(company_name: str, domain: str) -> str:
    """Run the outreach_copywriter agent for a single lead, return sanitized body."""
    task = build_draft_task(company_name, domain)
    crew = Crew(agents=[get_outreach_copywriter()], tasks=[task], verbose=False)
    raw_body = str(crew.kickoff())
    return sanitize_draft(raw_body)


# ---------------------------------------------------------------------------
# Batch orchestration
# ---------------------------------------------------------------------------


def run_draft_batch(
    conn: Optional[sqlite3.Connection] = None,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    delay_seconds: float = DRAFT_DELAY_SECONDS,
) -> dict:
    """
    Draft every 'Ready for Draft' lead.

    If conn is None, opens and owns a real on-disk connection via
    db.get_connection(), closing it before returning. Pass an
    in-memory connection explicitly for testing.

    A failure on one lead (logged) does not stop the batch and does
    not advance that lead's status, it stays 'Ready for Draft' and
    will be retried the next time this runs.
    """
    owns_connection = conn is None
    if conn is None:
        conn = db.get_connection()
        db.init_db(conn)

    try:
        ready_rows = db.get_leads_by_status(conn, "Ready for Draft")
        drafted = 0
        failed = 0

        for row in ready_rows:
            try:
                body = draft_one_email(row["company_name"], row["domain"])

                append_draft_to_file(
                    output_path,
                    company_name=row["company_name"],
                    domain=row["domain"],
                    email=row["contact_email"],
                    body=body,
                )
                db.update_status(conn, row["id"], "Drafted")
                drafted += 1

                word_count = len(body.split())
                if word_count > WORD_COUNT_WARNING_THRESHOLD:
                    logger.warning(
                        "Draft for '%s' ran long: %d words (target under 150)",
                        row["company_name"], word_count,
                    )
            except Exception as exc:  # noqa: BLE001 - any failure here must not kill the batch
                logger.error("Failed to draft email for '%s': %s", row["company_name"], exc)
                failed += 1

            time.sleep(delay_seconds)

        summary = {
            "attempted": len(ready_rows),
            "drafted": drafted,
            "failed": failed,
            "output_file": str(output_path),
        }
        logger.info("Draft run complete: %s", summary)
        return summary
    finally:
        if owns_connection:
            conn.close()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = run_draft_batch()
    print("\n--- Draft Run Summary ---")
    for key, value in result.items():
        print(f"{key}: {value}")