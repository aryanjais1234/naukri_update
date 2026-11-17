from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import inspect, text

from .config import settings, ROOT
from .database import Base, engine, get_db
from .models import CandidateProfile, JobListing, ApplicationRun
from .schemas import ProfileIn, ProfileOut, OpenUrlIn
from .browser.manager import browser_manager
from .application.router import router as application_router
from .jobs.router import router as jobs_router
from .agent.router import router as agent_router
from .naukri_refresh import router as naukri_refresh_router, rotator as naukri_rotator
from .naukri_resume_refresh import router as naukri_resume_refresh_router, refresher as naukri_resume_refresher

Base.metadata.create_all(bind=engine)
# Additive SQLite migrations for existing local installations.
with engine.begin() as connection:
    profile_columns = {col["name"] for col in inspect(engine).get_columns("candidate_profile")}
    migrations = {
        "application_preferences": "ALTER TABLE candidate_profile ADD COLUMN application_preferences TEXT NOT NULL DEFAULT '{}'",
        "resume_text": "ALTER TABLE candidate_profile ADD COLUMN resume_text TEXT NOT NULL DEFAULT ''",
        "resume_filename": "ALTER TABLE candidate_profile ADD COLUMN resume_filename VARCHAR(255) NOT NULL DEFAULT ''",
    }
    for column, statement in migrations.items():
        if column not in profile_columns:
            connection.execute(text(statement))

app = FastAPI(title="Naukri Update API", version="1.0.0")
app.include_router(application_router)
app.include_router(jobs_router)
app.include_router(agent_router)
app.include_router(naukri_refresh_router)
app.include_router(naukri_resume_refresh_router)
app.add_middleware(CORSMiddleware, allow_origins=[settings.frontend_origin],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
async def resume_naukri_rotation():
    await naukri_rotator.resume_if_enabled()
    await naukri_resume_refresher.resume_if_enabled()


@app.on_event("shutdown")
async def stop_naukri_rotation():
    await naukri_rotator.shutdown()
    await naukri_resume_refresher.shutdown()


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "Naukri Update", "version": "1.0.0"}


@app.get("/api/ai/status")
def ai_status():
    return {"provider": "OpenAI", "configured": bool(settings.openai_api_key),
            "model": settings.openai_model,
            "message": "Ready" if settings.openai_api_key else "Add OPENAI_API_KEY to backend/.env and restart."}


@app.get("/api/profile", response_model=ProfileOut)
def get_profile(db: Session = Depends(get_db)):
    profile = db.get(CandidateProfile, 1)
    if not profile:
        profile = CandidateProfile(id=1)
        db.add(profile)
        db.commit()
        db.refresh(profile)
    return profile


@app.put("/api/profile", response_model=ProfileOut)
def save_profile(payload: ProfileIn, db: Session = Depends(get_db)):
    profile = db.get(CandidateProfile, 1)
    if not profile:
        profile = CandidateProfile(id=1)
        db.add(profile)
    for key, value in payload.model_dump().items():
        setattr(profile, key, value)
    db.commit()
    db.refresh(profile)
    return profile


@app.post("/api/profile/resume")
async def upload_resume(file: UploadFile = File(...), db: Session = Depends(get_db)):
    filename = Path(file.filename or "resume").name
    suffix = Path(filename).suffix.lower()
    if suffix not in {".pdf", ".docx", ".txt"}:
        raise HTTPException(415, "Upload a PDF, DOCX or TXT resume.")
    raw = await file.read()
    if not raw or len(raw) > 12 * 1024 * 1024:
        raise HTTPException(413, "Resume must be non-empty and no larger than 12 MB.")
    resume_dir = ROOT / "data" / "resumes"
    resume_dir.mkdir(parents=True, exist_ok=True)
    path = resume_dir / f"{uuid.uuid4().hex}{suffix}"
    path.write_bytes(raw)
    try:
        if suffix == ".pdf":
            from pypdf import PdfReader
            from io import BytesIO
            reader = PdfReader(BytesIO(raw))
            extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
        elif suffix == ".docx":
            from docx import Document
            from io import BytesIO
            document = Document(BytesIO(raw))
            extracted = "\n".join(p.text for p in document.paragraphs)
            for table in document.tables:
                extracted += "\n" + "\n".join(" | ".join(cell.text for cell in row.cells) for row in table.rows)
        else:
            extracted = raw.decode("utf-8", errors="replace")
        extracted = extracted.strip()
        if not extracted:
            raise ValueError("No selectable text was found. This may be a scanned PDF; use a text-based resume.")
    except Exception as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(422, f"Could not read resume: {exc}") from exc

    profile = db.get(CandidateProfile, 1)
    if not profile:
        profile = CandidateProfile(id=1)
        db.add(profile)
    old_path = profile.resume_path
    profile.resume_path = str(path)
    profile.resume_filename = filename
    profile.resume_text = extracted[:50000]
    db.commit()
    # Remove previous uploaded resume only when it was inside JobPilot's own resume directory.
    if old_path:
        old = Path(old_path)
        if old.parent.resolve() == resume_dir.resolve() and old.exists() and old != path:
            old.unlink(missing_ok=True)
    return {"filename": filename, "resume_path": str(path), "extracted_characters": len(extracted)}


@app.get("/api/browser/status")
def browser_status():
    return browser_manager.status()


@app.post("/api/browser/start")
async def browser_start():
    try:
        return await browser_manager.start()
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@app.post("/api/browser/stop")
async def browser_stop():
    return await browser_manager.stop()


@app.post("/api/browser/open")
async def browser_open(payload: OpenUrlIn):
    try:
        return await browser_manager.open(payload.url)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
