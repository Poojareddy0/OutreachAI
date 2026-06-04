"""
gmail.py — OutreachAI
Merged Gmail module: OAuth flow, send, read, reply detection.
Previously 3 files (gmail_oauth.py, gmail_send.py, gmail_read.py).
"""

import base64
import json
import os
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")


# ── OAuth ─────────────────────────────────────────────────────────────────────

def connect_gmail() -> str:
    """Run OAuth flow in browser and return token JSON string for DB storage."""
    flow  = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
    creds = flow.run_local_server(port=0)
    return creds.to_json()


def load_credentials(token_json: str) -> Credentials:
    """Load credentials from stored JSON string, auto-refresh if expired."""
    creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


# ── Send ──────────────────────────────────────────────────────────────────────

def send_email(
    creds: Credentials,
    to_email: str,
    subject: str,
    body: str,
    thread_id: str = None,
    reply_to_msg_id: str = None,
) -> dict:
    """
    Send an email via Gmail API.

    thread_id        : keeps the message in an existing Gmail thread
    reply_to_msg_id  : RFC-2822 Message-ID of the original email.
                       Sets In-Reply-To + References so clients show
                       the follow-up inside the original conversation.
    """
    service = build("gmail", "v1", credentials=creds)

    message = EmailMessage()
    message["To"]      = to_email
    message["Subject"] = subject
    message.set_content(body)

    if reply_to_msg_id:
        message["In-Reply-To"] = reply_to_msg_id
        message["References"]  = reply_to_msg_id

    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode()
    payload = {"raw": encoded}
    if thread_id:
        payload["threadId"] = thread_id

    return service.users().messages().send(userId="me", body=payload).execute()


def get_message_id_header(creds: Credentials, gmail_message_id: str) -> str | None:
    """
    Fetch the RFC Message-ID header from a sent Gmail message.
    Stored in DB and used when sending follow-ups for proper threading.
    """
    try:
        service = build("gmail", "v1", credentials=creds)
        meta    = service.users().messages().get(
            userId="me",
            id=gmail_message_id,
            format="metadata",
            metadataHeaders=["Message-ID"],
        ).execute()
        for h in meta.get("payload", {}).get("headers", []):
            if h["name"] == "Message-ID":
                return h["value"]
    except Exception:
        pass
    return None


# ── Read / reply detection ────────────────────────────────────────────────────

def _header_map(headers: list) -> dict:
    return {h["name"]: h["value"] for h in headers}


def _get_thread_messages(creds: Credentials, thread_id: str) -> list:
    service = build("gmail", "v1", credentials=creds)
    thread  = service.users().threads().get(
        userId="me", id=thread_id, format="metadata"
    ).execute()
    return thread.get("messages", [])


def _parse_message_meta(message: dict) -> dict:
    headers     = _header_map(message.get("payload", {}).get("headers", []))
    date_header = headers.get("Date", "")
    dt = None
    if date_header:
        try:
            dt = parsedate_to_datetime(date_header)
        except Exception:
            pass
    return {
        "id":        message.get("id"),
        "thread_id": message.get("threadId"),
        "from":      headers.get("From", ""),
        "subject":   headers.get("Subject", ""),
        "date":      dt,
    }


def thread_has_reply(creds: Credentials, thread_id: str, user_email: str) -> tuple:
    """
    Returns (True, message_meta) if someone other than user_email
    has replied in the thread. Returns (False, None) otherwise.
    """
    messages         = _get_thread_messages(creds, thread_id)
    user_email_lower = user_email.lower()
    latest_external  = None

    if len(messages) <= 1:
        return False, None

    for msg in messages[1:]:
        meta       = _parse_message_meta(msg)
        from_lower = (meta["from"] or "").lower()
        if user_email_lower not in from_lower:
            if latest_external is None or (
                meta["date"] and latest_external["date"]
                and meta["date"] > latest_external["date"]
            ):
                latest_external = meta

    return latest_external is not None, latest_external
