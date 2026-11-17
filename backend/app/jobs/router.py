from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from html import unescape
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import CandidateProfile, JobListing
from ..browser.manager import browser_manager

router = APIRouter(prefix="/api/jobs", tags=["job discovery"])

class JobSearchIn(BaseModel):
    role: str = Field(min_length=2, max_length=120)
    location: str = ""
    experience: str = ""
    work_mode: str = "Any"
    platforms: list[str] = []
    limit: int = Field(default=40, ge=1, le=100)

class SaveJobIn(BaseModel):
    job_id: int


def clean_html(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", unescape(value)).strip()[:12000]

def stable_id(source: str, url: str, title: str, company: str) -> str:
    return hashlib.sha256(f"{source}|{url or title+'|'+company}".encode()).hexdigest()

def _experience_bounds(text: str) -> tuple[float, float | None] | None:
    text = (text or "").lower()
    patterns = [
        r"(?P<lo>\d+(?:\.\d+)?)\s*(?:-|–|—|to)\s*(?P<hi>\d+(?:\.\d+)?)\s*(?:years?|yrs?)",
        r"(?:at least|minimum(?: of)?|more than)\s*(?P<lo>\d+(?:\.\d+)?)\s*(?:years?|yrs?)",
        r"(?P<lo>\d+(?:\.\d+)?)\s*\+\s*(?:years?|yrs?)",
    ]
    for index, pattern in enumerate(patterns):
        match = re.search(pattern, text)
        if match:
            lower = float(match.group("lo"))
            upper = float(match.group("hi")) if "hi" in match.groupdict() and match.group("hi") else None
            if index == 1:
                upper = None
            return lower, upper
    return None


def _experience_filter_matches(job_text: str, requested: str) -> bool:
    """Only exclude on explicit experience requirements; unknown metadata stays visible."""
    requested = (requested or "").strip().lower()
    if not requested:
        return True
    selected = _experience_bounds(requested)
    required = _experience_bounds(job_text)
    if not selected or not required:
        return True
    selected_min, selected_max = selected
    required_min, required_max = required
    if selected_max is not None and required_min > selected_max:
        return False
    if required_max is not None and required_max < selected_min:
        return False
    return True


def matches(job: dict[str, Any], role: str, location: str, mode: str, experience: str = "") -> bool:
    haystack = " ".join([job.get("title", ""), job.get("description", ""), " ".join(job.get("tags", []))]).lower()
    terms = [t for t in re.split(r"[\s,/]+", role.lower()) if len(t) > 2]
    if terms and not any(t in haystack for t in terms):
        return False
    location_text = (job.get("location", "") + " " + job.get("description", "")).lower()
    if location and location.lower() not in location_text and location.lower() not in {"remote", "any"}:
        return False
    mode = (mode or "any").lower()
    work_text = (job.get("work_mode", "") + " " + job.get("location", "") + " " + job.get("description", "")).lower()
    known_modes = {"remote": bool(re.search(r"\bremote\b", work_text)),
                   "hybrid": bool(re.search(r"\bhybrid\b", work_text)),
                   "on-site": bool(re.search(r"\b(on[- ]?site|onsite)\b", work_text))}
    if mode in known_modes:
        if any(known_modes.values()) and not known_modes[mode]:
            return False
    if not _experience_filter_matches(job.get("title", "") + " " + job.get("description", ""), experience):
        return False
    return True

def score_job(job: dict[str, Any], profile: CandidateProfile | None) -> int:
    if not profile:
        return 0
    skills = [x.strip().lower() for x in re.split(r"[,;\n]", profile.skills or "") if x.strip()]
    text = (job.get("title", "") + " " + job.get("description", "")).lower()
    if not skills:
        return 0
    hits = sum(1 for skill in skills if skill in text)
    return min(100, round(100 * hits / min(len(skills), 8)))

def _slug(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def build_platform_search_url(platform: str, role: str, location: str, experience: str = "", work_mode: str = "Any") -> tuple[str, str]:
    """Create a native search destination for exactly one selected job board."""
    from urllib.parse import urlencode

    if platform == "LinkedIn":
        params = {"keywords": role}
        if location.strip():
            params["location"] = location.strip()
        return "https://www.linkedin.com/jobs/search/?" + urlencode(params), "LinkedIn search is prefilled with the requested role and location. Refine experience and work mode with LinkedIn filters."

    if platform == "Naukri":
        role_slug = _slug(role)
        # Naukri's location slug is generally the city, not "City, Country".
        location_value = (location or "").split(",", 1)[0].strip()
        location_slug = _slug(location_value)
        path = f"/{role_slug}-jobs"
        if location_slug and location_slug not in {"any", "india"}:
            path += f"-in-{location_slug}"
        return "https://www.naukri.com" + path, "Naukri's native role/city search URL is prefilled. Refine experience and work mode with Naukri's own filters."

    if platform == "Indeed":
        params = {"q": role}
        if location.strip():
            params["l"] = location.strip()
        return "https://in.indeed.com/jobs?" + urlencode(params), "Indeed search is prefilled with the requested role and location. Refine experience and work mode with Indeed filters."

    if platform == "Wellfound":
        # Wellfound documents /jobs as its search entry point and its filters are
        # applied inside the signed-in UI; undocumented query parameters are avoided.
        hint = (
            f"Wellfound Jobs opened. Use its native filters for role: '{role}', "
            f"location: '{location or 'Any location'}', "
            f"experience: '{experience or 'Any experience'}' and work mode: '{work_mode}'."
        )
        return "https://wellfound.com/jobs", hint

    raise ValueError(f"Unsupported job platform: {platform}")


@router.post("/search")
async def search_jobs(payload: JobSearchIn, db: Session = Depends(get_db)):
    """Launch native searches on the selected platform(s), never substitute feeds.

    This endpoint intentionally returns no jobs from unrelated sources. The selected
    site is the search source; user navigates its native results and then moves to the
    employer's direct application/ATS page for JobPilot's application agent.
    """
    allowed = {"LinkedIn", "Naukri", "Indeed", "Wellfound"}
    selected = list(dict.fromkeys(payload.platforms or []))
    if not selected:
        raise HTTPException(400, "Select at least one job platform before searching.")
    unknown = [name for name in selected if name not in allowed]
    if unknown:
        raise HTTPException(400, "Unsupported platform selection: " + ", ".join(unknown))

    searches = []
    for platform in selected:
        url, hint = build_platform_search_url(platform, payload.role, payload.location, payload.experience, payload.work_mode)
        searches.append({"platform": platform, "url": url, "search_hint": hint})

    try:
        opened = await browser_manager.open_searches(searches)
    except Exception as exc:
        raise HTTPException(502, f"Could not launch selected platform search: {type(exc).__name__}: {exc}") from exc

    opened_by_name = {item["platform"]: item for item in opened}
    links = []
    errors = []
    for search in searches:
        result = opened_by_name.get(search["platform"], {})
        item = {**search, **{k: result[k] for k in ("status", "title", "error") if k in result}}
        links.append(item)
        if result.get("status") != "opened":
            errors.append(f"{search['platform']}: {result.get('error', 'The browser did not confirm navigation.')}")

    successful = [item["platform"] for item in links if item.get("status") == "opened"]
    return {
        "count": 0,
        "jobs": [],
        "sources_used": successful,
        "errors": errors,
        "platform_links": links,
        "selected_platforms": selected,
        "search_mode": "native_platform_browser",
        "note": (
            "Only the selected platform search page(s) were opened. No Remotive, Arbeitnow, "
            "or other third-party job feed was queried. LinkedIn, Naukri and Indeed searches "
            "use their native search URLs. Wellfound opens its documented Jobs page; apply "
            "role, location, experience and work-mode filters in its native UI. After choosing a listing, "
            "open the employer's direct application/Workday page and start JobPilot there."
        ),
    }


@router.post("/import-current")
async def import_current_page(db: Session = Depends(get_db)):
    """Create a local job record from a user-opened employer application URL.

    This intentionally refuses LinkedIn/Indeed/Naukri/Wellfound pages. The user must
    navigate to the employer's direct application URL before importing it.
    """
    from urllib.parse import urlparse
    from datetime import datetime, timezone
    import hashlib
    if not browser_manager.page:
        raise HTTPException(400, "Open the employer's application page in the JobPilot browser first.")
    url = browser_manager.page.url
    host = (urlparse(url).hostname or "").lower()
    restricted = ("linkedin.com", "indeed.com", "naukri.com", "wellfound.com")
    if any(host == d or host.endswith("." + d) for d in restricted):
        raise HTTPException(400, "Open the employer's direct application/ATS URL. Job-board pages are not imported or scraped.")
    title = (await browser_manager.page.title())[:300]
    body = (await browser_manager.page.locator("body").inner_text(timeout=2500))[:9000]
    external_id = "manual:" + hashlib.sha256(url.encode()).hexdigest()
    row = db.query(JobListing).filter(JobListing.external_id == external_id).first()
    if not row:
        row = JobListing(external_id=external_id, saved_at=datetime.now(timezone.utc).isoformat())
        db.add(row)
    row.title = title or "Employer application"
    row.company = host
    row.location = ""
    row.source = "Manual browser"
    row.url = url
    row.description = clean_html(body)
    db.commit(); db.refresh(row)
    return {"id": row.id, "title": row.title, "company": row.company, "url": row.url,
            "source": row.source, "message": "Current employer page saved. You can now start the application agent."}


@router.get("")
def list_jobs(limit: int = 100, db: Session = Depends(get_db)):
    rows=db.query(JobListing).order_by(JobListing.saved_at.desc()).limit(min(max(limit,1),200)).all()
    return {"count":len(rows),"jobs":[{"id":x.id,"title":x.title,"company":x.company,"location":x.location,"source":x.source,"url":x.url,"description":x.description[:800],"posted_at":x.posted_at,"work_mode":x.work_mode,"salary":x.salary,"match_score":x.match_score} for x in rows]}
