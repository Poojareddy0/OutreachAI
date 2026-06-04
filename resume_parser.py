"""
resume_parser.py — OutreachAI
AI-powered resume parsing and job match analysis using Gemini 2.5 Flash.
Used by both app.py and resend.py — kept as a separate file for that reason.
"""

import logging
import os

from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from pypdf import PdfReader

load_dotenv()

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _client


# ── Schemas ───────────────────────────────────────────────────────────────────

class ResumeProfile(BaseModel):
    summary: str = Field(
        description="2-3 sentence professional summary of the candidate."
    )
    skills: list[str] = Field(
        description="All technical skills, tools, languages, cloud platforms, and frameworks found."
    )
    education: str = Field(
        description="Highest degree and institution. Example: M.Eng. CS, University of Cincinnati."
    )
    experience_snippet: str = Field(
        description="2-3 sentence summary of key work experience and most impactful contributions."
    )
    years_of_experience: str = Field(
        description="Estimated total years of experience. Example: '2 years'."
    )


class JobMatch(BaseModel):
    match_percentage: int = Field(
        description="How well the candidate matches the job, from 0 to 100."
    )
    matched_skills: list[str] = Field(
        description="Skills the candidate has that the job explicitly requires."
    )
    missing_keywords: list[str] = Field(
        description="Skills the job requires that are missing or weak in the resume."
    )
    experience_gaps: list[str] = Field(
        description="Experience areas the job needs that the resume does not show well."
    )
    improvement_suggestions: list[str] = Field(
        description="3-5 specific, actionable suggestions to improve the resume for this exact role."
    )
    bullet_suggestions: list[str] = Field(
        description="2-3 example resume bullet points the candidate could add."
    )
    strength_summary: str = Field(
        description="1-2 sentences on what the candidate does well for this role."
    )


# ── PDF extraction ────────────────────────────────────────────────────────────

def extract_text(uploaded_file) -> str:
    try:
        reader = PdfReader(uploaded_file)
        text   = ""
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
        return text.strip()
    except Exception as e:
        return f"Error reading resume: {e}"


# ── AI resume parsing ─────────────────────────────────────────────────────────

def parse_resume(text: str) -> dict:
    """Parse resume text into structured profile using Gemini."""
    try:
        response = _get_client().models.generate_content(
            model="gemini-2.5-flash-preview-05-20",
            contents=(
                "Parse this resume carefully and extract structured information. "
                "Be thorough with skills — include every tool, language, platform, framework.\n\n"
                f"{text[:6000]}"
            ),
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ResumeProfile,
                temperature=0.1,
            ),
        )
        data: ResumeProfile = response.parsed
        smart_background = (
            f"Summary: {data.summary}\n"
            f"Skills: {', '.join(data.skills)}\n"
            f"Education: {data.education}\n"
            f"Experience: {data.experience_snippet}\n"
            f"Years of experience: {data.years_of_experience}"
        )
        return {
            "summary":             data.summary,
            "skills":              data.skills,
            "education":           data.education,
            "experience":          data.experience_snippet,
            "years_of_experience": data.years_of_experience,
            "smart_background":    smart_background,
            "full_text":           text,
        }
    except Exception as e:
        logging.error("Resume parsing error: %s", e)
        return {
            "summary": "", "skills": [], "education": "", "experience": "",
            "years_of_experience": "", "smart_background": "", "full_text": text,
        }


# ── AI job match ──────────────────────────────────────────────────────────────

def analyze_match(parsed_resume: dict, job_text: str, job_title: str = "") -> dict:
    """Analyze how well the resume matches a job description using Gemini."""
    empty = {
        "matched_skills": [], "missing_keywords": [], "experience_gaps": [],
        "improvement_suggestions": [], "bullet_suggestions": [],
        "strength_summary": "", "match_percentage": 0,
    }
    if not job_text or not job_text.strip():
        return empty

    try:
        prompt = (
            f"Analyze how well this candidate matches the job. Be specific and accurate.\n\n"
            f"Target role: {job_title}\n\n"
            f"Candidate profile:\n{parsed_resume.get('smart_background', '')}\n\n"
            f"Job description:\n{job_text[:4000]}"
        )
        response = _get_client().models.generate_content(
            model="gemini-2.5-flash-preview-05-20",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=JobMatch,
                temperature=0.1,
            ),
        )
        res: JobMatch = response.parsed
        return {
            "matched_skills":          res.matched_skills,
            "missing_keywords":        res.missing_keywords,
            "experience_gaps":         res.experience_gaps,
            "improvement_suggestions": res.improvement_suggestions,
            "bullet_suggestions":      res.bullet_suggestions,
            "strength_summary":        res.strength_summary,
            "match_percentage":        res.match_percentage,
        }
    except Exception as e:
        logging.error("Job match error: %s", e)
        return empty
