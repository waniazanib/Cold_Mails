"""
gmail_draft.py
Saves cold email drafts directly into your Gmail Drafts folder.

Nothing is sent automatically. Every draft lands in Gmail > Drafts,
you review it, attach anything extra you want, and hit Send yourself.
The Gmail API scope used is 'gmail.compose' only, the narrowest
possible, it cannot read, delete, or send any email.

OAuth2 flow:
  1. First run opens a browser tab asking you to approve access.
  2. Approval saves a token.json locally, never needs repeating.
  3. token.json auto-refreshes when it expires, no browser needed again.

Place credentials.json (downloaded from Google Cloud Console) in the
project root before running for the first time. See README setup steps.

Dependencies: google-auth, google-auth-oauthlib, google-api-python-client
Expected location: src/gmail_draft.py
"""

import base64
import logging
import mimetypes
import os
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

logger = logging.getLogger("gmail_draft")

# ---------------------------------------------------------------------------
# Paths and config
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CREDENTIALS_PATH = PROJECT_ROOT / "credentials.json"
TOKEN_PATH = PROJECT_ROOT / "token.json"

# Narrowest possible scope: compose only, cannot read/send/delete.
SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]

LINKEDIN_URL = os.getenv("LINKEDIN_URL", "")
GITHUB_URL = os.getenv("GITHUB_URL", "")
RESUME_PATH = Path(os.getenv("RESUME_PATH", "resumes/resume_ai.pdf"))
if not RESUME_PATH.is_absolute():
    RESUME_PATH = PROJECT_ROOT / RESUME_PATH


# ---------------------------------------------------------------------------
# OAuth2
# ---------------------------------------------------------------------------


def get_gmail_service():
    """
    Return an authenticated Gmail API service object. Handles token
    refresh automatically, and only opens a browser on the very first
    run (or if token.json is deleted).
    """
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"credentials.json not found at {CREDENTIALS_PATH}. "
            f"Download it from Google Cloud Console > APIs & Services > "
            f"Credentials > OAuth 2.0 Client ID > Download JSON, then "
            f"place it in the project root."
        )

    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_PATH), SCOPES
            )
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")

    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# Email construction
# ---------------------------------------------------------------------------


def build_link_footer() -> str:
    """
    Build the LinkedIn / GitHub footer appended to every draft.
    Skips a link silently if its env var was not set.
    """
    lines = []
    if LINKEDIN_URL:
        lines.append(f"LinkedIn: {LINKEDIN_URL}")
    if GITHUB_URL:
        lines.append(f"GitHub: {GITHUB_URL}")
    if not lines:
        return ""
    return "\n\n" + "\n".join(lines)


def build_mime_message(
    to: str,
    subject: str,
    body: str,
    resume_path: Optional[Path] = None,
) -> MIMEMultipart:
    """
    Assemble a MIME email with the draft body, link footer, and an
    optional resume attachment. Plain text only, no HTML, so the email
    reads cleanly in any client and does not trigger spam filters.
    """
    message = MIMEMultipart()
    message["to"] = to
    message["subject"] = subject

    full_body = body.strip() + build_link_footer()
    message.attach(MIMEText(full_body, "plain"))

    resume = resume_path or RESUME_PATH
    if resume and resume.exists():
        mime_type, _ = mimetypes.guess_type(str(resume))
        maintype, subtype = (mime_type or "application/octet-stream").split("/", 1)
        with open(resume, "rb") as f:
            attachment = MIMEApplication(f.read(), _subtype=subtype)
        attachment.add_header(
            "Content-Disposition", "attachment", filename=resume.name
        )
        message.attach(attachment)
        logger.debug("Attached resume: %s", resume.name)
    else:
        if resume:
            logger.warning(
                "Resume file not found at %s, draft will be saved without attachment.",
                resume,
            )

    return message


def encode_message(message: MIMEMultipart) -> dict:
    """Encode a MIME message to the base64url format Gmail API expects."""
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    return {"message": {"raw": raw}}


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------


def create_gmail_draft(
    to: str,
    subject: str,
    body: str,
    resume_path: Optional[Path] = None,
) -> Optional[str]:
    """
    Save one cold email as a Gmail draft. Returns the draft id on
    success, or None on failure (caller logs and continues the batch).

    Args:
        to:          Recipient email address.
        subject:     Email subject line.
        body:        Plain text body from the copywriter agent.
        resume_path: Override the default RESUME_PATH for this draft.
    """
    try:
        service = get_gmail_service()
        mime_msg = build_mime_message(to, subject, body, resume_path)
        encoded = encode_message(mime_msg)
        draft = service.users().drafts().create(userId="me", body=encoded).execute()
        draft_id = draft.get("id")
        logger.info("Gmail draft saved (id=%s) for recipient: %s", draft_id, to)
        return draft_id
    except FileNotFoundError as exc:
        logger.error("Gmail setup incomplete: %s", exc)
        return None
    except HttpError as exc:
        logger.error("Gmail API error saving draft for %s: %s", to, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error saving Gmail draft for %s: %s", to, exc)
        return None


# ---------------------------------------------------------------------------
# Manual smoke test (run once to verify OAuth2 and attachment)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    test_to = input("Test recipient email: ").strip()
    draft_id = create_gmail_draft(
        to=test_to,
        subject="Test Draft - Cold Mails Pipeline",
        body="This is a test draft. If you see this in Gmail Drafts, the pipeline is working.",
    )
    if draft_id:
        print(f"Success, check Gmail Drafts. Draft id: {draft_id}")
    else:
        print("Failed, check logs above.")