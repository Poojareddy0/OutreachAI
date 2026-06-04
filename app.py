"""
app.py — OutreachAI
────────────────────
AI-powered recruiter outreach, resume matching, and follow-up automation.
Auth logic merged in (was previously auth.py).

Run: streamlit run app.py
"""

import hashlib
import hmac
import logging
import os
import re
from datetime import datetime

import pandas as pd
import plotly.express as px
import streamlit as st
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from database import (
    init_db, get_user_by_id, get_user_by_email, create_user,
    save_gmail_token, clear_gmail_token, update_user_name,
    email_exists, insert_email, get_all_emails, get_email_by_id, update_email_notes,
    get_stats, get_status_counts, get_job_type_counts,
    get_company_counts, get_emails_over_time,
    save_template, get_templates, delete_template,
)
from gmail import connect_gmail, load_credentials, send_email, get_message_id_header
from resume_parser import extract_text, parse_resume, analyze_match

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    filename="app.log",
    filemode="a",
    format="%(asctime)s - %(levelname)s - %(message)s",
)

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _client


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _hash_password(password: str) -> str:
    salt = os.urandom(16)
    key  = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100000)
    return salt.hex() + ":" + key.hex()


def _verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt_hex, key_hex = stored_hash.split(":")
        salt         = bytes.fromhex(salt_hex)
        expected_key = bytes.fromhex(key_hex)
        actual_key   = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100000)
        return hmac.compare_digest(actual_key, expected_key)
    except Exception:
        return False


def _signup(name: str, email: str, password: str):
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    create_user(name, email, _hash_password(password))


def _login(email: str, password: str):
    user = get_user_by_email(email)
    if user and _verify_password(password, user["password_hash"]):
        return dict(user)
    return None


def _logout():
    for k in ["user", "parsed_resume", "subject", "body", "allow_resend",
              "send_company", "send_job_title", "send_job_post", "match_data", "_last_jd"]:
        st.session_state.pop(k, None)


# ── Auth page ─────────────────────────────────────────────────────────────────

def auth_page():
    st.markdown("<h1 style='text-align:center;'>✉️ OutreachAI</h1>", unsafe_allow_html=True)
    st.markdown(
        "<p style='text-align:center;color:gray;font-size:1.05em;'>"
        "AI-powered recruiter outreach · Resume matching · Auto follow-ups"
        "</p><br>",
        unsafe_allow_html=True,
    )

    tab1, tab2 = st.tabs(["Login", "Sign Up"])

    with tab1:
        login_email    = st.text_input("Email", key="login_email")
        login_password = st.text_input("Password", type="password", key="login_password")
        if st.button("Login", use_container_width=True):
            if not login_email or not login_password:
                st.warning("Please enter both email and password.")
            else:
                user = _login(login_email, login_password)
                if user:
                    st.session_state.user = user
                    st.rerun()
                else:
                    st.error("Invalid email or password.")

    with tab2:
        signup_name     = st.text_input("Full Name", key="signup_name")
        signup_email    = st.text_input("Email", key="signup_email")
        signup_password = st.text_input("Password (min 8 chars)", type="password", key="signup_password")
        if st.button("Create Account", use_container_width=True):
            if not signup_name or not signup_email or not signup_password:
                st.warning("Please fill in all fields.")
            else:
                try:
                    _signup(signup_name, signup_email, signup_password)
                    st.success("Account created! Please log in.")
                except ValueError as ve:
                    st.warning(str(ve))
                except Exception:
                    st.error("This email is already registered.")


# ── Email generation ──────────────────────────────────────────────────────────

class EmailOutput(BaseModel):
    subject: str = Field(description="Professional email subject line.")
    body:    str = Field(description="Full candidate outreach email body with proper line breaks.")


def _generate_email(prompt: str) -> tuple[str | None, str | None]:
    try:
        response = _get_client().models.generate_content(
            model="gemini-2.5-flash-preview-05-20",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=EmailOutput,
                temperature=0.7,
            ),
        )
        data: EmailOutput = response.parsed
        subject = data.subject.strip()
        body    = data.body.strip()
        if subject and body:
            return subject, body
    except Exception as e:
        logging.error("Email generation error: %s", e)
    return None, None


# ── UI helpers ────────────────────────────────────────────────────────────────

def _is_valid_email(email: str) -> bool:
    return bool(re.match(r"^[\w\.-]+@[\w\.-]+\.\w+$", email))


def _pct_color(pct: int) -> str:
    return "#27ae60" if pct >= 70 else "#f39c12" if pct >= 40 else "#e74c3c"


def _pct_label(pct: int) -> str:
    return "Strong Match" if pct >= 70 else "Partial Match" if pct >= 40 else "Weak Match"


# ── Page: Send Email ──────────────────────────────────────────────────────────

def send_email_page(user):
    st.subheader("✉️ Send Email")

    if "allow_resend" not in st.session_state:
        st.session_state.allow_resend = False

    if not user["gmail_connected"]:
        st.warning("⚠️ Connect your Gmail account in **Settings** before sending.")
        return

    left, right = st.columns([1.2, 0.8])

    with left:
        st.markdown("#### Recruiter Details")
        name      = st.text_input("Recruiter Name *", key="send_name")
        email     = st.text_input("Recruiter Email *", key="send_email_addr")
        company   = st.text_input("Company Name", key="send_company_input")
        job_title = st.text_input(
            "Job Title *",
            value=st.session_state.get("send_job_title", ""),
            key="send_job_title_input",
        )
        job_type  = st.selectbox(
            "Job Type",
            ["Data Engineering", "Analytics Engineering", "AI / ML Engineering",
             "Data Analysis", "BI / Reporting", "Software Engineering", "Other"],
            key="send_job_type",
        )

        st.markdown("#### Job Description")
        job_post = st.text_area(
            "Paste the job description *(optional — improves email quality)*",
            value=st.session_state.get("send_job_post", ""),
            height=150, key="send_jd",
        )

        st.markdown("#### Your Resume")
        uploaded = st.file_uploader("Upload PDF", type=["pdf"], key="send_resume")
        if uploaded:
            with st.spinner("Parsing resume with Gemini..."):
                text = extract_text(uploaded)
                if text.startswith("Error reading resume:"):
                    st.error(text)
                else:
                    parsed = parse_resume(text)
                    st.session_state.parsed_resume = parsed
                    st.success(f"✅ Resume parsed — {len(parsed.get('skills', []))} skills found")

        additional = st.text_area("Additional Notes (optional)", height=65, key="send_notes")

    parsed_resume = st.session_state.get("parsed_resume", {})

    # Run match analysis once per unique JD
    if parsed_resume and job_post.strip():
        if st.session_state.get("_last_jd") != job_post:
            with st.spinner("Analyzing job match..."):
                st.session_state.match_data = analyze_match(parsed_resume, job_post, job_title)
                st.session_state._last_jd   = job_post

    match_data = st.session_state.get("match_data", {
        "matched_skills": [], "missing_keywords": [], "experience_gaps": [],
        "improvement_suggestions": [], "bullet_suggestions": [],
        "strength_summary": "", "match_percentage": 0,
    })

    background = parsed_resume.get("smart_background", "")
    if additional.strip():
        background += f"\n\nAdditional notes: {additional.strip()}"

    with right:
        if parsed_resume:
            st.markdown("#### Candidate Profile")
            st.text_area(
                "Extracted from your resume",
                value=parsed_resume.get("smart_background", ""),
                height=190, disabled=True, key="send_profile_display",
            )
            if job_post.strip() and match_data.get("match_percentage"):
                pct   = match_data["match_percentage"]
                color = _pct_color(pct)
                label = _pct_label(pct)
                st.markdown(
                    f"<h3 style='color:{color};'>{pct}% — {label}</h3>",
                    unsafe_allow_html=True,
                )
                if match_data.get("strength_summary"):
                    st.info(match_data["strength_summary"])
                if match_data.get("matched_skills"):
                    st.write("**✅ Matched:** " + ", ".join(match_data["matched_skills"][:6]))
                if match_data.get("missing_keywords"):
                    st.write("**⚠️ Missing:** " + ", ".join(match_data["missing_keywords"][:5]))
        else:
            st.info("Upload your resume to enable AI email generation.")

    st.markdown("---")

    # Validation
    can_generate = True
    if email and not _is_valid_email(email):
        st.error("Invalid email address.")
        can_generate = False
    if email and email_exists(user["id"], email) and not st.session_state.allow_resend:
        st.warning(f"You've already emailed **{email}**.")
        if st.button("Send again anyway"):
            st.session_state.allow_resend = True
            st.rerun()
        can_generate = False
    if not name or not email or not job_title:
        st.caption("* Required fields must be filled.")
        can_generate = False
    if not background.strip():
        can_generate = False

    prompt = f"""
Write a professional cold outreach email from a job candidate to a recruiter.

Candidate: {user["name"]}
Recruiter: {name}
Company: {company or "the company"}
Target role: {job_title}
Job type: {job_type}
Candidate profile:
{background}
Job match: {match_data.get("match_percentage", "N/A")}%
Strength summary: {match_data.get("strength_summary") or "See profile"}
Strong skills: {", ".join(match_data.get("matched_skills", [])) or "See profile"}
Experience gaps: {", ".join(match_data.get("experience_gaps", [])) or "None"}
Job description: {job_post or "Not provided"}

Rules:
- First person as the CANDIDATE reaching out to the recruiter
- Professional, confident, and specific — mention company and role by name
- Highlight 2-3 specific relevant strengths
- 5-7 lines in the body
- End with: Best regards,\\n{user["name"]}
- Use \\n for line breaks
- Never write as if you are the recruiter or hiring manager
"""

    gen_col, tmpl_col = st.columns([2, 1])

    with gen_col:
        if can_generate and st.button("🤖 Generate Email with Gemini", use_container_width=True):
            with st.spinner("Generating..."):
                subject, body = _generate_email(prompt)
            if body:
                bad = ["we are hiring", "we're hiring", "i am hiring",
                       "our team is hiring", "we have an opening"]
                if any(p in body.lower() for p in bad):
                    subject, body = None, None
            if subject and body:
                if user["name"] not in body:
                    body = body.strip() + f"\n\nBest regards,\n{user['name']}"
                st.session_state.subject = subject
                st.session_state.body    = body
            else:
                st.error("Generation failed. Please try again.")

    with tmpl_col:
        templates = get_templates(user["id"])
        if templates:
            tmpl_names = ["— Load template —"] + [t["name"] for t in templates]
            selected   = st.selectbox("Load Template", tmpl_names, key="load_tmpl")
            if selected != "— Load template —":
                tmpl = next(t for t in templates if t["name"] == selected)
                st.session_state.subject = tmpl["subject"]
                st.session_state.body    = tmpl["body"]
                st.rerun()

    if "subject" in st.session_state:
        st.markdown("### ✏️ Review & Send")
        subject = st.text_input("Subject", st.session_state.subject, key="preview_subject")
        body    = st.text_area("Body", st.session_state.body, height=250, key="preview_body")

        save_col, send_col = st.columns([1, 2])
        with save_col:
            tmpl_name = st.text_input("Save as template", key="tmpl_name_input")
            if st.button("💾 Save Template"):
                if tmpl_name.strip():
                    save_template(user["id"], tmpl_name.strip(), subject, body)
                    st.success(f"Saved as '{tmpl_name}'.")
                else:
                    st.warning("Enter a template name first.")

        with send_col:
            if st.button("📤 Send Email", use_container_width=True, type="primary"):
                with st.spinner("Sending via Gmail..."):
                    creds      = load_credentials(user["gmail_token_json"])
                    sent_msg   = send_email(creds, email, subject, body)
                    msg_id     = get_message_id_header(creds, sent_msg["id"])
                    insert_email(
                        user_id=user["id"], recruiter_name=name,
                        recruiter_email=email, company=company, job_title=job_title,
                        job_type=job_type, background=background, subject=subject,
                        body=body, sent_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        thread_id=sent_msg.get("threadId"), message_id=msg_id,
                    )
                st.success(f"✅ Email sent to **{name}** at {company or email}!")
                for k in ["subject", "body", "send_job_title", "send_job_post",
                          "match_data", "_last_jd"]:
                    st.session_state.pop(k, None)
                st.session_state.allow_resend = False
                st.rerun()


# ── Page: Job Match Analyzer ──────────────────────────────────────────────────

def job_match_page():
    st.subheader("🔍 Job Match Analyzer")
    st.caption("Upload your resume and paste a job description for an AI-powered match analysis.")

    col1, col2 = st.columns([1.2, 0.8])
    with col1:
        job_title = st.text_input("Target Job Title", key="analyzer_title")
        job_post  = st.text_area("Job Description", height=210, key="analyzer_jd",
                                  placeholder="Paste the full job description here")
        uploaded  = st.file_uploader("Upload Resume (PDF)", type=["pdf"], key="analyzer_resume")
        parsed    = {}
        if uploaded:
            with st.spinner("Parsing resume..."):
                text = extract_text(uploaded)
                if text.startswith("Error reading resume:"):
                    st.error(text)
                else:
                    parsed = parse_resume(text)
                    st.success(f"✅ {len(parsed.get('skills', []))} skills found")

    with col2:
        if parsed and job_post.strip():
            with st.spinner("Analyzing with Gemini..."):
                match = analyze_match(parsed, job_post, job_title)
            pct   = match.get("match_percentage", 0)
            color = _pct_color(pct)
            label = _pct_label(pct)
            st.markdown(
                f"<h2 style='color:{color};text-align:center;'>{pct}%</h2>"
                f"<p style='text-align:center;color:{color};font-weight:600;'>{label}</p>",
                unsafe_allow_html=True,
            )
            if match.get("strength_summary"):
                st.success(match["strength_summary"])
            st.write("**✅ Matched:** " + (", ".join(match["matched_skills"]) or "None"))
            st.write("**⚠️ Missing:** " + (", ".join(match["missing_keywords"]) or "None"))
            if match.get("experience_gaps"):
                st.write("**📌 Experience gaps:** " + ", ".join(match["experience_gaps"]))
            if match.get("improvement_suggestions"):
                st.markdown("#### Improvement Suggestions")
                for i, s in enumerate(match["improvement_suggestions"], 1):
                    st.write(f"{i}. {s}")
            if match.get("bullet_suggestions"):
                st.markdown("#### Suggested Bullets to Add")
                for b in match["bullet_suggestions"]:
                    st.markdown(f"> {b}")
        else:
            st.info("Upload your resume and paste a job description to see the full analysis.")


# ── Page: Resume Improvement ──────────────────────────────────────────────────

def resume_improvement_page():
    st.subheader("📄 Resume Improvement Assistant")
    st.caption("Get AI suggestions on how to tailor your resume for a specific role.")

    left, right = st.columns([1.2, 0.8])
    with left:
        job_title = st.text_input("Target Job Title", key="improve_title")
        job_post  = st.text_area("Job Description", height=210, key="improve_jd",
                                  placeholder="Paste the full job description here")
        uploaded  = st.file_uploader("Upload Resume (PDF)", type=["pdf"], key="improve_resume")

    parsed = {}
    if uploaded:
        with st.spinner("Parsing resume..."):
            text = extract_text(uploaded)
            if text.startswith("Error reading resume:"):
                st.error(text)
            else:
                parsed = parse_resume(text)
                st.success(f"✅ {len(parsed.get('skills', []))} skills detected")

    with right:
        if parsed and job_post.strip():
            with st.spinner("Analyzing..."):
                match = analyze_match(parsed, job_post, job_title)
            pct = match.get("match_percentage", 0)
            st.metric("Resume Match Score", f"{pct}%")
            if match.get("strength_summary"):
                st.success(match["strength_summary"])
            st.write("**⚠️ Missing skills:** " + (", ".join(match["missing_keywords"][:8]) or "None"))
            st.write("**📌 Experience gaps:** " + (", ".join(match["experience_gaps"][:8]) or "None"))
            if match.get("improvement_suggestions"):
                st.markdown("#### What to Improve")
                for i, s in enumerate(match["improvement_suggestions"], 1):
                    st.write(f"{i}. {s}")
            if match.get("bullet_suggestions"):
                st.markdown("#### Bullets to Add to Your Resume")
                for b in match["bullet_suggestions"]:
                    st.markdown(f"> {b}")
            st.markdown("#### Your Extracted Profile")
            st.text_area("From resume", value=parsed.get("smart_background", ""),
                         height=180, disabled=True, key="improve_profile")
        else:
            st.info("Upload your resume and paste a job description to get suggestions.")


# ── Page: Dashboard ───────────────────────────────────────────────────────────

def dashboard_page(user):
    st.subheader("📊 Dashboard")

    total, followups, pending, replied = get_stats(user["id"])
    reply_rate = f"{round(replied / total * 100)}%" if total > 0 else "—"

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Sent",    total)
    c2.metric("Follow-ups",    followups)
    c3.metric("Pending Reply", pending)
    c4.metric("Replied",       replied)
    c5.metric("Reply Rate",    reply_rate)

    st.markdown("---")

    col1, col2 = st.columns(2)
    with col1:
        status_data = get_status_counts(user["id"])
        if status_data:
            labels = {"sent": "Sent", "followup_sent": "Follow-up Sent", "replied": "Replied"}
            df = pd.DataFrame(
                [(labels.get(r[0], r[0]), r[1]) for r in status_data],
                columns=["Status", "Count"],
            )
            fig = px.pie(df, names="Status", values="Count",
                         title="Email Status Breakdown",
                         color_discrete_sequence=px.colors.qualitative.Set2, hole=0.4)
            fig.update_layout(margin=dict(t=40, b=0, l=0, r=0))
            st.plotly_chart(fig, use_container_width=True)

    with col2:
        job_data = get_job_type_counts(user["id"])
        if job_data:
            df2 = pd.DataFrame(job_data, columns=["Job Type", "Count"])
            fig2 = px.bar(df2, x="Job Type", y="Count", title="Emails by Role Type",
                          color="Job Type",
                          color_discrete_sequence=px.colors.qualitative.Pastel)
            fig2.update_layout(showlegend=False, margin=dict(t=40, b=0, l=0, r=0))
            st.plotly_chart(fig2, use_container_width=True)

    col3, col4 = st.columns(2)
    with col3:
        timeline = get_emails_over_time(user["id"])
        if timeline:
            df3 = pd.DataFrame(timeline, columns=["Date", "Count"])
            fig3 = px.line(df3, x="Date", y="Count", title="Emails Sent Over Time",
                           markers=True, color_discrete_sequence=["#3498db"])
            fig3.update_layout(margin=dict(t=40, b=0, l=0, r=0))
            st.plotly_chart(fig3, use_container_width=True)

    with col4:
        company_data = get_company_counts(user["id"])
        if company_data:
            df4 = pd.DataFrame(company_data, columns=["Company", "Count"])
            fig4 = px.bar(df4, x="Count", y="Company", title="Top Companies Contacted",
                          orientation="h", color_discrete_sequence=["#9b59b6"])
            fig4.update_layout(margin=dict(t=40, b=0, l=0, r=0))
            st.plotly_chart(fig4, use_container_width=True)

    st.markdown("---")
    st.markdown("### Email History")

    emails = get_all_emails(user["id"])
    if not emails:
        st.info("No emails sent yet.")
        return

    status_labels = {
        "sent": "📤 Sent", "followup_sent": "📨 Follow-up Sent", "replied": "✅ Replied"
    }
    rows = [
        {
            "ID":            dict(r)["id"],
            "Recruiter":     dict(r)["recruiter_name"],
            "Email":         dict(r)["recruiter_email"],
            "Company":       dict(r).get("company") or "—",
            "Job Title":     dict(r).get("job_title") or "—",
            "Status":        status_labels.get(dict(r).get("status", ""), dict(r).get("status", "")),
            "Sent At":       dict(r)["sent_at"],
            "Follow-up At":  dict(r).get("followup_at") or "—",
            "Reply At":      dict(r).get("reply_detected_at") or "—",
        }
        for r in emails
    ]

    filter_status = st.selectbox(
        "Filter by Status",
        ["All", "📤 Sent", "📨 Follow-up Sent", "✅ Replied"],
        key="hist_filter",
    )
    df = pd.DataFrame(rows)
    if filter_status != "All":
        df = df[df["Status"] == filter_status]

    st.dataframe(df.drop(columns=["ID"]), use_container_width=True)

    st.markdown("### Add Notes")
    if rows:
        selected_id = st.selectbox(
            "Select email",
            [r["ID"] for r in rows],
            format_func=lambda x: f"#{x} — {next((r['Recruiter'] for r in rows if r['ID']==x), '')} @ {next((r['Company'] for r in rows if r['ID']==x), '')}",
            key="notes_select",
        )
        existing = get_email_by_id(selected_id)
        notes = st.text_area(
            "Notes (interview dates, feedback, reminders…)",
            value=existing["notes"] if existing and existing["notes"] else "",
            height=90, key="notes_input",
        )
        if st.button("Save Notes"):
            update_email_notes(selected_id, notes)
            st.success("Notes saved.")


# ── Page: Templates ───────────────────────────────────────────────────────────

def templates_page(user):
    st.subheader("📋 Email Templates")
    st.caption("Save and reuse your best-performing email drafts.")

    templates = get_templates(user["id"])
    if not templates:
        st.info("No saved templates yet. Generate an email on the Send Email page and save it as a template.")
        return

    for tmpl in [dict(t) for t in templates]:
        with st.expander(f"📄 {tmpl['name']}  ·  saved {tmpl['created_at'][:10]}"):
            st.text_input("Subject", value=tmpl["subject"], disabled=True, key=f"ts_{tmpl['id']}")
            st.text_area("Body", value=tmpl["body"], height=170, disabled=True, key=f"tb_{tmpl['id']}")
            if st.button("🗑️ Delete", key=f"td_{tmpl['id']}"):
                delete_template(tmpl["id"])
                st.rerun()


# ── Page: Settings ────────────────────────────────────────────────────────────

def settings_page(user):
    st.subheader("⚙️ Settings")

    st.markdown("### Profile")
    new_name = st.text_input("Name", value=user["name"], key="edit_name")
    st.text_input("Login Email", value=user["email"], disabled=True)
    if st.button("Update Name"):
        if new_name.strip():
            update_user_name(user["id"], new_name.strip())
            st.success("Name updated.")
            st.rerun()
        else:
            st.error("Name cannot be empty.")

    st.markdown("---")
    st.markdown("### Gmail Account")
    if user["gmail_connected"]:
        st.success("✅ Gmail connected")
        st.caption(f"Account: {user['email']}")
        if st.button("Disconnect Gmail"):
            clear_gmail_token(user["id"])
            st.rerun()
    else:
        st.info("No Gmail account connected.")
        if st.button("Connect Gmail", type="primary"):
            with st.spinner("Opening browser for Google authentication..."):
                save_gmail_token(user["id"], connect_gmail())
            st.success("Gmail connected!")
            st.rerun()

    st.markdown("---")
    st.markdown("### Follow-up Automation")
    st.write("Checks for replies and sends follow-ups after 3 days to non-responding recruiters.")
    st.code("python3 resend.py", language="bash")
    st.write("Automate with a daily cron job (runs at 9 AM):")
    st.code("0 9 * * * cd /path/to/outreachai && python3 resend.py >> cron.log 2>&1", language="bash")

    st.markdown("---")
    st.markdown("### Security")
    st.warning("⚠️ Never commit `credentials.json` or `.env` to GitHub.")
    st.code("echo 'credentials.json' >> .gitignore\necho '.env' >> .gitignore", language="bash")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    st.set_page_config(page_title="OutreachAI", page_icon="✉️", layout="centered")
    init_db()

    if "user" not in st.session_state:
        st.session_state.user = None

    if not st.session_state.user:
        auth_page()
        return

    user = dict(get_user_by_id(st.session_state.user["id"]))

    st.markdown("<h1 style='text-align:center;'>✉️ OutreachAI</h1>", unsafe_allow_html=True)
    st.markdown(
        "<p style='text-align:center;color:gray;'>"
        "AI-powered recruiter outreach · Resume matching · Auto follow-ups"
        "</p>",
        unsafe_allow_html=True,
    )

    _, _, pending, _ = get_stats(user["id"])

    st.sidebar.markdown("## ✉️ OutreachAI")
    st.sidebar.markdown(f"👋 Hi, **{user['name']}**")
    if pending > 0:
        st.sidebar.info(f"📨 {pending} email(s) awaiting reply")
    st.sidebar.markdown("---")

    page = st.sidebar.radio(
        "Navigate",
        ["✉️ Send Email", "🔍 Job Match Analyzer", "📄 Resume Improvement",
         "📊 Dashboard", "📋 Templates", "⚙️ Settings"],
    )

    st.sidebar.markdown("---")
    if st.sidebar.button("Logout", use_container_width=True):
        _logout()
        st.rerun()

    pages = {
        "✉️ Send Email":          lambda: send_email_page(user),
        "🔍 Job Match Analyzer":  job_match_page,
        "📄 Resume Improvement":  resume_improvement_page,
        "📊 Dashboard":           lambda: dashboard_page(user),
        "📋 Templates":           lambda: templates_page(user),
        "⚙️ Settings":            lambda: settings_page(user),
    }
    pages[page]()


if __name__ == "__main__":
    main()
