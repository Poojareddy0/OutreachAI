"""
database.py — OutreachAI
All SQLite access goes through this file only.
"""

import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME  = os.path.join(BASE_DIR, "outreachai.db")


def get_connection():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn   = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        name             TEXT NOT NULL,
        email            TEXT NOT NULL UNIQUE,
        password_hash    TEXT NOT NULL,
        gmail_connected  INTEGER DEFAULT 0,
        gmail_token_json TEXT,
        created_at       TEXT DEFAULT (datetime('now'))
    )""")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS emails (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id           INTEGER NOT NULL,
        recruiter_name    TEXT NOT NULL,
        recruiter_email   TEXT NOT NULL,
        company           TEXT,
        job_title         TEXT,
        job_type          TEXT,
        background        TEXT,
        subject           TEXT,
        body              TEXT,
        status            TEXT DEFAULT 'sent',
        sent_at           TEXT,
        followup_at       TEXT,
        thread_id         TEXT,
        message_id        TEXT,
        reply_detected_at TEXT,
        notes             TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )""")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS templates (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL,
        name       TEXT NOT NULL,
        subject    TEXT,
        body       TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY(user_id) REFERENCES users(id)
    )""")

    # Safe migrations for existing databases
    cursor.execute("PRAGMA table_info(emails)")
    existing = {r[1] for r in cursor.fetchall()}
    for col, typedef in {
        "thread_id": "TEXT", "reply_detected_at": "TEXT", "message_id": "TEXT",
        "company": "TEXT", "job_title": "TEXT", "notes": "TEXT",
    }.items():
        if col not in existing:
            cursor.execute(f"ALTER TABLE emails ADD COLUMN {col} {typedef}")

    conn.commit()
    conn.close()


# ── Users ─────────────────────────────────────────────────────────────────────

def create_user(name, email, password_hash):
    conn = get_connection()
    conn.execute(
        "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
        (name, email, password_hash),
    )
    conn.commit()
    conn.close()


def get_user_by_email(email):
    conn = get_connection()
    user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()
    return user


def get_user_by_id(user_id):
    conn = get_connection()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return user


def save_gmail_token(user_id, token_json):
    conn = get_connection()
    conn.execute(
        "UPDATE users SET gmail_connected = 1, gmail_token_json = ? WHERE id = ?",
        (token_json, user_id),
    )
    conn.commit()
    conn.close()


def clear_gmail_token(user_id):
    conn = get_connection()
    conn.execute(
        "UPDATE users SET gmail_connected = 0, gmail_token_json = NULL WHERE id = ?",
        (user_id,),
    )
    conn.commit()
    conn.close()


def update_user_name(user_id, new_name):
    conn = get_connection()
    conn.execute("UPDATE users SET name = ? WHERE id = ?", (new_name, user_id))
    conn.commit()
    conn.close()


# ── Emails ────────────────────────────────────────────────────────────────────

def email_exists(user_id, recruiter_email):
    conn   = get_connection()
    result = conn.execute(
        "SELECT 1 FROM emails WHERE user_id = ? AND recruiter_email = ?",
        (user_id, recruiter_email),
    ).fetchone()
    conn.close()
    return result is not None


def insert_email(user_id, recruiter_name, recruiter_email, company, job_title,
                 job_type, background, subject, body, sent_at,
                 thread_id=None, message_id=None):
    conn = get_connection()
    conn.execute("""
    INSERT INTO emails
      (user_id, recruiter_name, recruiter_email, company, job_title,
       job_type, background, subject, body, status, sent_at, thread_id, message_id)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'sent', ?, ?, ?)
    """, (user_id, recruiter_name, recruiter_email, company, job_title,
          job_type, background, subject, body, sent_at, thread_id, message_id))
    conn.commit()
    conn.close()


def get_all_emails(user_id):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM emails WHERE user_id = ? ORDER BY id DESC", (user_id,)
    ).fetchall()
    conn.close()
    return rows


def get_email_by_id(email_id):
    conn = get_connection()
    row  = conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
    conn.close()
    return row


def update_email_notes(email_id, notes):
    conn = get_connection()
    conn.execute("UPDATE emails SET notes = ? WHERE id = ?", (notes, email_id))
    conn.commit()
    conn.close()


def get_stats(user_id):
    conn      = get_connection()
    total     = conn.execute("SELECT COUNT(*) FROM emails WHERE user_id = ?", (user_id,)).fetchone()[0]
    followups = conn.execute("SELECT COUNT(*) FROM emails WHERE user_id = ? AND status='followup_sent'", (user_id,)).fetchone()[0]
    pending   = conn.execute("SELECT COUNT(*) FROM emails WHERE user_id = ? AND status='sent'", (user_id,)).fetchone()[0]
    replied   = conn.execute("SELECT COUNT(*) FROM emails WHERE user_id = ? AND status='replied'", (user_id,)).fetchone()[0]
    conn.close()
    return total, followups, pending, replied


def get_status_counts(user_id):
    conn = get_connection()
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM emails WHERE user_id = ? GROUP BY status", (user_id,)
    ).fetchall()
    conn.close()
    return rows


def get_job_type_counts(user_id):
    conn = get_connection()
    rows = conn.execute(
        "SELECT job_type, COUNT(*) FROM emails WHERE user_id = ? GROUP BY job_type", (user_id,)
    ).fetchall()
    conn.close()
    return rows


def get_company_counts(user_id):
    conn = get_connection()
    rows = conn.execute(
        """SELECT company, COUNT(*) FROM emails
           WHERE user_id = ? AND company IS NOT NULL
           GROUP BY company ORDER BY COUNT(*) DESC LIMIT 10""",
        (user_id,),
    ).fetchall()
    conn.close()
    return rows


def get_emails_over_time(user_id):
    conn = get_connection()
    rows = conn.execute(
        """SELECT substr(sent_at,1,10) as day, COUNT(*)
           FROM emails WHERE user_id = ?
           GROUP BY day ORDER BY day""",
        (user_id,),
    ).fetchall()
    conn.close()
    return rows


def get_pending_followups():
    conn = get_connection()
    rows = conn.execute("""
    SELECT e.*, u.name AS user_name, u.gmail_token_json
    FROM emails e JOIN users u ON e.user_id = u.id
    WHERE e.status = 'sent'
      AND u.gmail_connected = 1
      AND u.gmail_token_json IS NOT NULL
    ORDER BY e.sent_at ASC
    """).fetchall()
    conn.close()
    return rows


def update_followup_status(email_id, followup_time):
    conn = get_connection()
    conn.execute(
        "UPDATE emails SET status = 'followup_sent', followup_at = ? WHERE id = ?",
        (followup_time, email_id),
    )
    conn.commit()
    conn.close()


def mark_replied(email_id, replied_time):
    conn = get_connection()
    conn.execute(
        "UPDATE emails SET status = 'replied', reply_detected_at = ? WHERE id = ?",
        (replied_time, email_id),
    )
    conn.commit()
    conn.close()


# ── Templates ─────────────────────────────────────────────────────────────────

def save_template(user_id, name, subject, body):
    conn = get_connection()
    conn.execute(
        "INSERT INTO templates (user_id, name, subject, body) VALUES (?, ?, ?, ?)",
        (user_id, name, subject, body),
    )
    conn.commit()
    conn.close()


def get_templates(user_id):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM templates WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
    ).fetchall()
    conn.close()
    return rows


def delete_template(template_id):
    conn = get_connection()
    conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))
    conn.commit()
    conn.close()
