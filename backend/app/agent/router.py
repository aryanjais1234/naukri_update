from __future__ import annotations

import json
from fastapi import APIRouter, Depends, HTTPException
from datetime import datetime, timezone
from urllib.parse import urlparse
import hashlib
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db, SessionLocal
from ..models import CandidateProfile, JobListing, ApplicationRun
from ..browser.manager import browser_manager
from .engine import application_agent

router = APIRouter(prefix="/api/agent", tags=["application agent"])


class AgentStartIn(BaseModel):
    job_ids: list[int] = Field(min_length=1, max_length=25)
    auto_submit: bool = True


class AgentStartCurrentIn(BaseModel):
    auto_submit: bool = True


class AgentContinueIn(BaseModel):
    skip_optional_keys: list[str] = Field(default_factory=list, max_length=80)


@router.post("/start")
async def start_agent(payload: AgentStartIn, db: Session = Depends(get_db)):
    if not settings.openai_api_key:
        raise HTTPException(400, "OpenAI is not configured. Add OPENAI_API_KEY to backend/.env and restart the backend.")
    profile = db.get(CandidateProfile, 1)
    if not profile or not profile.resume_path:
        raise HTTPException(400, "Upload your resume in Candidate profile and save your profile before starting the agent.")
    from pathlib import Path
    if not Path(profile.resume_path).is_file():
        raise HTTPException(400, "The saved resume file is missing. Upload it again in Candidate profile.")
    jobs = db.query(JobListing).filter(JobListing.id.in_(payload.job_ids)).all()
    found = {job.id for job in jobs}
    missing = [job_id for job_id in payload.job_ids if job_id not in found]
    if missing:
        raise HTTPException(404, f"Job records not found: {missing}")
    try:
        return await application_agent.start(payload.job_ids, payload.auto_submit)
    except Exception as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/start-current")
async def start_current_page(payload: AgentStartCurrentIn, db: Session = Depends(get_db)):
    if not settings.openai_api_key:
        raise HTTPException(400, "OpenAI is not configured. Add OPENAI_API_KEY to backend/.env and restart the backend.")
    profile = db.get(CandidateProfile, 1)
    if not profile or not profile.resume_path:
        raise HTTPException(400, "Upload your resume in Candidate profile and save your profile before starting the agent.")
    from pathlib import Path
    if not Path(profile.resume_path).is_file():
        raise HTTPException(400, "The saved resume file is missing. Upload it again in Candidate profile.")
    if not browser_manager.page:
        raise HTTPException(400, "Launch the JobPilot browser and open a direct employer/ATS application page first.")
    url = browser_manager.page.url
    host = (urlparse(url).hostname or "").lower()
    restricted = ("linkedin.com", "indeed.com", "naukri.com", "wellfound.com")
    if any(host == domain or host.endswith("." + domain) for domain in restricted):
        raise HTTPException(400, "Navigate to the employer's direct application URL first. Job-board pages are not automated.")
    title = (await browser_manager.page.title())[:300] or "Employer application"
    body = (await browser_manager.page.locator("body").inner_text(timeout=2500))[:9000]
    external_id = "manual:" + hashlib.sha256(url.encode()).hexdigest()
    row = db.query(JobListing).filter(JobListing.external_id == external_id).first()
    if not row:
        row = JobListing(external_id=external_id, saved_at=datetime.now(timezone.utc).isoformat())
        db.add(row)
    row.title = title
    row.company = host
    row.source = "Manual browser"
    row.url = url
    row.description = body
    db.commit()
    db.refresh(row)
    try:
        return await application_agent.start([row.id], payload.auto_submit)
    except Exception as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/continue")
async def continue_agent(payload: AgentContinueIn | None = None):
    try:
        return await application_agent.continue_after_handoff((payload or AgentContinueIn()).skip_optional_keys)
    except Exception as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/stop")
async def stop_agent():
    return await application_agent.stop()


@router.get("/status")
def agent_status():
    return application_agent.snapshot()


@router.get("/runs")
def list_runs(limit: int = 100, db: Session = Depends(get_db)):
    rows = db.query(ApplicationRun).order_by(ApplicationRun.id.desc()).limit(min(max(limit, 1), 200)).all()
    return {"count": len(rows), "runs": [
        {"id": row.id, "job_id": row.job_id, "status": row.status, "step": row.step,
         "message": row.message, "details": _parse_details(row.details),
         "created_at": row.created_at, "updated_at": row.updated_at, "submitted_at": row.submitted_at}
        for row in rows
    ]}


def _parse_details(raw: str):
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
