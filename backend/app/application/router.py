from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from ..database import get_db
from ..models import CandidateProfile
from ..browser.manager import browser_manager
from .form_engine import inspect_page, prepare_fill
from .ats_detector import detect_ats
from .workflow import inspect_workflow, advance_workflow
from ..ai.application_mapper import map_application_fields

router = APIRouter(prefix="/api/application", tags=["application"])


def profile_dict(db: Session):
    p = db.get(CandidateProfile, 1)
    if not p:
        raise HTTPException(400, "Save your candidate profile first.")
    return {k: getattr(p, k) for k in ["full_name", "email", "phone", "location", "experience_years",
        "current_employer", "current_role", "skills", "preferred_roles", "preferred_locations",
        "expected_compensation", "notice_period", "resume_path", "resume_filename", "resume_text",
        "application_preferences"]}


@router.get("/inspect")
async def inspect_current_page():
    if not browser_manager.page:
        raise HTTPException(400, "Launch the managed browser and open an application page first.")
    try:
        result = await inspect_page(browser_manager.page)
        result["ats"] = await detect_ats(browser_manager.page)
        result["workflow"] = await inspect_workflow(browser_manager.page, result["ats"])
        return result
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@router.post("/suggest")
async def suggest_current_page(db: Session = Depends(get_db)):
    if not browser_manager.page:
        raise HTTPException(400, "Launch the managed browser and open an application page first.")
    profile = profile_dict(db)
    try:
        inspected = await inspect_page(browser_manager.page)
        ats = await detect_ats(browser_manager.page)
        result = await map_application_fields(inspected.get("fields", []), profile)
        result["ats"] = ats
        result["url"] = browser_manager.page.url
        return result
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@router.get("/workflow")
async def current_workflow():
    if not browser_manager.page:
        raise HTTPException(400, "Launch the managed browser and open an application page first.")
    try:
        ats = await detect_ats(browser_manager.page)
        return await inspect_workflow(browser_manager.page, ats)
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@router.post("/workflow/next")
async def continue_workflow():
    if not browser_manager.page:
        raise HTTPException(400, "Launch the managed browser and open an application page first.")
    try:
        ats = await detect_ats(browser_manager.page)
        result = await advance_workflow(browser_manager.page, ats)
        result["inspection"] = await inspect_page(browser_manager.page)
        return result
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@router.post("/prepare")
async def prepare_current_page(payload: dict, db: Session = Depends(get_db)):
    if not browser_manager.page:
        raise HTTPException(400, "Launch the managed browser and open an application page first.")
    profile = profile_dict(db)
    try:
        inspected = await inspect_page(browser_manager.page)
        valid_keys = {f["key"] for f in inspected.get("fields", [])}
        answers = {k: v for k, v in payload.get("answers", {}).items() if k in valid_keys}
        result = await prepare_fill(browser_manager.page, inspected, profile, answers)
        result["ats"] = await detect_ats(browser_manager.page)
        return result
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc
