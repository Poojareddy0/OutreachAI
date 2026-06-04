"""
resend.py — OutreachAI Follow-up Engine
────────────────────────────────────────
Standalone cron script. Never imports from app.py.

- Checks all sent emails older than FOLLOWUP_DAYS
- Detects replies via Gmail thread read → marks as replied, skips
- Otherwise generates a Gemini follow-up and sends it with proper threading

Run manually : python3 resend.py
Cron (9 AM)  : 0 9 * * * cd /path/to/outreachai && python3 resend.py >> cron.log 2>&1
"""

import logging
import os
from datetime import datetime, timedelta

from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from database import (
    init_db, get_pending_followups, get_user_by_id,
    update_followup_status, mark_replied,
)
from gmail import load_credentials, send_email, thread_has_reply

load_dotenv()

FOLLOWUP_DAYS = 3

logging.basicConfig(
    level=logging.INFO,
    filename="resend.log",
    filemode="a",
    format="%(asctime)s - %(levelname)s - %(message)s",
)

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _client


class FollowUpEmail(BaseModel):
    subject: str = Field(description="Follow-up email subject line.")
    body:    str = Field(description="Follow-up email body. Professional, 4-6 lines.")


def generate_followup(
    user_name: str, recruiter_name: str, company: str,
    original_subject: str, job_title: str, background: str,
) -> tuple[str | None, str | None]:
    try:
        prompt = (
            f"Write a brief professional follow-up email from a job candidate to a recruiter.\n\n"
            f"Candidate: {user_name}\n"
            f"Recruiter: {recruiter_name}\n"
            f"Company: {company or 'the company'}\n"
            f"Original email subject: {original_subject}\n"
            f"Target role: {job_title or 'a data/engineering role'}\n"
            f"Candidate background: {background}\n\n"
            f"Rules:\n"
            f"- First person from candidate's perspective\n"
            f"- Reference the previous email naturally\n"
            f"- Politely ask if there are any updates\n"
            f"- Mention continued interest in the role\n"
            f"- 4-6 lines total\n"
            f"- End with: Best regards,\\n{user_name}\n"
            f"- Use \\n for line breaks\n"
            f"- Reference company and role specifically — not generic"
        )
        response = _get_client().models.generate_content(
            model="gemini-2.5-flash-preview-05-20",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=FollowUpEmail,
                temperature=0.7,
            ),
        )
        data: FollowUpEmail = response.parsed
        subject = data.subject.strip()
        body    = data.body.strip()
        if subject and body:
            return subject, body
    except Exception as e:
        logging.error("Follow-up generation error: %s", e)
    return None, None


def should_followup(sent_time: str) -> bool:
    sent_dt = datetime.strptime(sent_time, "%Y-%m-%d %H:%M:%S")
    return datetime.now() >= sent_dt + timedelta(days=FOLLOWUP_DAYS)


def process_followups():
    init_db()
    rows = get_pending_followups()

    if not rows:
        print("No pending follow-ups.")
        logging.info("No pending follow-ups found.")
        return

    for row in rows:
        email_id         = row["id"]
        user_id          = row["user_id"]
        recruiter_name   = row["recruiter_name"]
        recruiter_email  = row["recruiter_email"]
        company          = row.get("company") or ""
        job_title        = row.get("job_title") or row.get("job_type") or ""
        background       = row["background"] or ""
        original_subject = row["subject"] or ""
        sent_time        = row["sent_at"]
        thread_id        = row["thread_id"]
        message_id       = row.get("message_id")
        user_name        = row["user_name"] or "Candidate"
        gmail_token_json = row["gmail_token_json"]

        print(f"\nChecking: {recruiter_email} (sent {sent_time})")

        if not should_followup(sent_time):
            print(f"  Not yet {FOLLOWUP_DAYS} days. Skipping.")
            continue

        creds      = load_credentials(gmail_token_json)
        user       = get_user_by_id(user_id)
        user_email = user["email"] if user else ""

        # Detect reply — skip follow-up if recruiter already responded
        if thread_id:
            has_reply, _ = thread_has_reply(creds, thread_id, user_email)
            if has_reply:
                mark_replied(email_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                print(f"  Reply detected! Marked as replied.")
                logging.info("Reply detected: email_id=%d from %s", email_id, recruiter_email)
                continue

        subject, body = generate_followup(
            user_name, recruiter_name, company,
            original_subject, job_title, background,
        )

        if not subject or not body:
            print(f"  Could not generate follow-up.")
            logging.error("Generation failed for email_id=%d", email_id)
            continue

        if user_name not in body:
            body = body.strip() + f"\n\nBest regards,\n{user_name}"

        try:
            send_email(
                creds=creds,
                to_email=recruiter_email,
                subject=subject,
                body=body,
                thread_id=thread_id,
                reply_to_msg_id=message_id,
            )
            update_followup_status(email_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            print(f"  Follow-up sent to {recruiter_email}")
            logging.info("Follow-up sent: email_id=%d to %s", email_id, recruiter_email)
        except Exception as e:
            print(f"  Send error: {e}")
            logging.error("Send error email_id=%d %s: %s", email_id, recruiter_email, e)

    print("\nFollow-up process complete.")
    logging.info("Follow-up run complete.")


if __name__ == "__main__":
    process_followups()
