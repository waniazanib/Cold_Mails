"""
crew_copywriter.py
Phase 4 orchestration: Segmented Copy Generation.

Pulls every 'Ready for Draft' lead, runs the outreach_copywriter agent
to write a cold email, appends it to output/cold_email_drafts.md,
saves it as a Gmail draft (with resume attached and LinkedIn/GitHub
links in the footer), and flips status to 'Drafted'.

Gmail draft creation is non-fatal: if it fails for any lead (missing
credentials.json, API error, etc.) the markdown file still gets the
draft and the lead still gets marked Drafted. The summary dict includes
a gmail_drafted count so you can see exactly what landed in Gmail vs
what needs to be copy-pasted manually.

A per-lead LLM failure leaves the lead at 'Ready for Draft' for retry.

Dependencies: crewai, google-auth-oauthlib, google-auth-httplib2,
              google-api-python-client
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
from gmail_draft import create_gmail_draft

logger = logging.getLogger("crew_copywriter")

DEFAULT_OUTPUT_PATH = Path(__file__).resolve().parent.parent / "output" / "cold_email_drafts.md"

DRAFT_DELAY_SECONDS = 2.0
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
    text = re.sub(r",\s*,", ",", text)
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
# Email subject line builder
# ---------------------------------------------------------------------------


def build_subject(company_name: str) -> str:
    """
    Build a subject line for the cold email. Kept short and direct,
    no 'Re:' prefix or spam-trigger words like 'Opportunity' or 'Urgent'.
    """
    clean = company_name.split("(")[0].strip()  # drop parenthetical suffixes
    return f"AI Engineer Application - {clean}"


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

    For each lead:
    1. Generates the cold email body via the LLM agent
    2. Appends to cold_email_drafts.md (always, before Gmail attempt)
    3. Creates a Gmail draft with resume attached and links in footer
    4. Flips status to 'Drafted'

    An LLM failure leaves the lead at 'Ready for Draft' for retry.
    A Gmail failure is logged but the lead is still marked Drafted
    since the markdown file already has the content.
    """
    owns_connection = conn is None
    if conn is None:
        conn = db.get_connection()
        db.init_db(conn)

    try:
        ready_rows = db.get_leads_by_status(conn, "Ready for Draft")
        drafted = 0
        failed = 0
        gmail_drafted = 0

        for row in ready_rows:
            try:
                body = draft_one_email(row["company_name"], row["domain"])

                # Step 1: always write to markdown first so content is
                # never lost even if the Gmail step fails
                append_draft_to_file(
                    output_path,
                    company_name=row["company_name"],
                    domain=row["domain"],
                    email=row["contact_email"],
                    body=body,
                )

                # Step 2: save to Gmail Drafts if we have a contact email
                if row["contact_email"]:
                    subject = build_subject(row["company_name"])
                    draft_id = create_gmail_draft(
                        to=row["contact_email"],
                        subject=subject,
                        body=body,
                    )
                    if draft_id:
                        gmail_drafted += 1
                    else:
                        logger.warning(
                            "Gmail draft not created for '%s', "
                            "copy from cold_email_drafts.md manually.",
                            row["company_name"],
                        )
                else:
                    logger.info(
                        "No email address for '%s', skipping Gmail draft.",
                        row["company_name"],
                    )

                # Step 3: mark complete regardless of Gmail outcome
                db.update_status(conn, row["id"], "Drafted")
                drafted += 1

                word_count = len(body.split())
                if word_count > WORD_COUNT_WARNING_THRESHOLD:
                    logger.warning(
                        "Draft for '%s' ran long: %d words (target under 150)",
                        row["company_name"], word_count,
                    )

            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Failed to draft email for '%s': %s", row["company_name"], exc
                )
                failed += 1

            time.sleep(delay_seconds)

        summary = {
            "attempted": len(ready_rows),
            "drafted": drafted,
            "gmail_drafted": gmail_drafted,
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