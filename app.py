from ollama import Client
import ollama
import streamlit as st
import re
import csv
from csv import writer
from datetime import datetime
import os
import logging
import uuid
import smtplib
from email.message import EmailMessage
import json
import re as regex

# ------------------ CONFIGURATION ------------------
SENDER_EMAIL = "reddydani09@gmail.com"      # your Gmail
APP_PASSWORD = "lseo sgxd plwa vktw"                  # Gmail App Password
# -------------------------------------------------

# Logging
logging.basicConfig(
    level=logging.INFO,
    filename="app.log",
    filemode="a",
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# Ollama
client = Client(host="http://localhost:11434")
model = "llama3.2"

# ---------------- HELPERS ------------------

def is_valid_email(email):
    pattern = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'
    return re.match(pattern, email)

def is_email_in_logs(email):
    try:
        with open("logs.csv", "r", newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                if row and row[2] == email:
                    return True
    except FileNotFoundError:
        return False
    return False

def send_email(subject, body, recipient, attachment=None):
    try:
        msg = EmailMessage()
        msg["From"] = SENDER_EMAIL
        msg["To"] = recipient
        msg["Subject"] = subject
        msg.set_content(body)

        if attachment and os.path.exists(attachment):
            with open(attachment, "rb") as f:
                msg.add_attachment(
                    f.read(),
                    maintype="application",
                    subtype="pdf",
                    filename=os.path.basename(attachment),
                )

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SENDER_EMAIL, APP_PASSWORD)
            server.send_message(msg)

        logging.info(f"Email sent to {recipient}")
        return True

    except Exception as e:
        logging.error(f"Email send failed: {e}")
        return False

def check_body(body):
    if not body:
        return ""
    return body.strip()

# ---------------- OLLAMA EMAIL GENERATION ------------------

def generate_email(system_prompt, question):
    try:
        response = ollama.chat(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question},
            ],
        )

        content = response["message"]["content"]

        # SAFE JSON EXTRACTION
        match = regex.search(r"\{[\s\S]*\}", content)
        if not match:
            raise ValueError("No JSON found in response")

        data = json.loads(match.group(0))

        subject = data.get("subject")
        body = data.get("body")

        if not subject or not body:
            raise ValueError("Missing subject/body")

        return subject.strip(), body.strip()

    except Exception as e:
        logging.error(f"Ollama error: {e}")
        st.error(f"Ollama error: {e}")
        return None, None

# ------------------ STREAMLIT APP ------------------

def main():
    st.title("Auto Email Sender for Job Applications")

    prof_name = st.text_input("Enter professor name")
    prof_mail = st.text_input("Enter professor email")
    job_description = st.text_area("Enter job description")

    if prof_mail and not is_valid_email(prof_mail):
        st.error("Invalid email format")
        return

    job_type = st.radio("Job Type", ("Technical", "Non-Technical"))

    if not prof_name or not prof_mail or not job_description:
        st.warning("Fill all fields")
        return

    if is_email_in_logs(prof_mail):
        st.warning("Email already sent to this address")
        return

    about_me = "I am a motivated graduate student seeking opportunities."

    system_prompt = f"""
    You are a professional email writer.
    Generate a formal {job_type.lower()} on-campus job email.
    Return ONLY JSON with keys "subject" and "body".
    """

    question = f"Write an email to {prof_name} regarding {job_description}. {about_me}"

    if st.button("Generate Email"):
        subject, body = generate_email(system_prompt, question)
        body = check_body(body)

        if subject and body:
            st.session_state.subject = subject
            st.session_state.body = body
            st.success("Email generated")

    if "subject" in st.session_state:
        st.subheader("Generated Email")
        st.text_area(
            "Preview",
            st.session_state.subject + "\n\n" + st.session_state.body,
            height=300,
        )

        if st.button("Send Email"):
            if send_email(st.session_state.subject, st.session_state.body, prof_mail):
                with open("logs.csv", "a", newline="") as f:
                    writer(f).writerow([
                        str(uuid.uuid4()),
                        prof_name,
                        prof_mail,
                        job_type,
                        job_description,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    ])
                st.success("Email sent & logged")
            else:
                st.error("Failed to send email")

# ------------------ RUN ----------------

if __name__ == "__main__":
    main()
