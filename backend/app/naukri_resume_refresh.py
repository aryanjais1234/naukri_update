from __future__ import annotations

import asyncio
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from .config import ROOT
from .browser.manager import browser_manager
from .database import SessionLocal
from .models import CandidateProfile

PROFILE_URL = "https://www.naukri.com/mnjuser/profile"
STATE_PATH = ROOT / "data" / "naukri_resume_refresh.json"
ALLOWED_INTERVAL_HOURS = (1, 3, 6, 24)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_token(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9.-]+", "_", value.strip())
    return value.strip("_.-")


def _experience_token(value: str) -> str:
    match = re.search(r"\d+(?:\.\d+)?", value or "")
    if not match:
        raise ValueError("Set Total experience in Candidate profile before configuring resume refresh.")
    return match.group(0).replace(".", "p")


def build_structured_resume_name(full_name: str, experience_years: str, suffix: str, today: str | None = None) -> str:
    parts = [_safe_token(part) for part in (full_name or "").split()]
    parts = [part for part in parts if part]
    if not parts:
        raise ValueError("Set your full name in Candidate profile before configuring resume refresh.")
    first = parts[0]
    last = "_".join(parts[1:]) if len(parts) > 1 else ""
    yoe = _experience_token(experience_years)
    date = today or datetime.now().astimezone().strftime("%Y-%m-%d")
    name_parts = [first]
    if last:
        name_parts.append(last)
    name_parts.extend([f"{yoe}_YOE", date])
    return "_".join(name_parts) + suffix.lower()


class ConfigureIn(BaseModel):
    interval_hours: int = 3

    @field_validator("interval_hours")
    @classmethod
    def validate_interval(cls, value: int) -> int:
        if value not in ALLOWED_INTERVAL_HOURS:
            raise ValueError("Choose an update interval of 1, 3, 6 or 24 hours.")
        return value


class NaukriResumeRefresher:
    """Re-upload the candidate's current resume to their signed-in Naukri profile."""

    def __init__(self) -> None:
        self.interval_hours = 3
        self.enabled = False
        self.last_updated = ""
        self.last_filename = ""
        self.last_status = "idle"
        self.last_message = "Configure a resume and interval to begin."
        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.schedule_changed = asyncio.Event()
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            interval = int(data.get("interval_hours", 3))
            self.interval_hours = interval if interval in ALLOWED_INTERVAL_HOURS else 3
            self.enabled = bool(data.get("enabled", False))
            self.last_updated = str(data.get("last_updated", ""))
            self.last_filename = str(data.get("last_filename", ""))
            self.last_status = str(data.get("last_status", "idle"))
            self.last_message = str(data.get("last_message", ""))
        except Exception:
            self.enabled = False

    def _save(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps({
            "interval_hours": self.interval_hours,
            "enabled": self.enabled,
            "last_updated": self.last_updated,
            "last_filename": self.last_filename,
            "last_status": self.last_status,
            "last_message": self.last_message,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def _profile_data(self) -> dict[str, str]:
        db = SessionLocal()
        try:
            profile = db.get(CandidateProfile, 1)
            if not profile:
                raise ValueError("Complete Candidate profile and upload a resume first.")
            if not profile.resume_path or not Path(profile.resume_path).is_file():
                raise ValueError("Upload a resume in Candidate profile before starting automatic refresh.")
            return {
                "full_name": profile.full_name or "",
                "experience_years": profile.experience_years or "",
                "resume_path": profile.resume_path,
                "resume_filename": profile.resume_filename or Path(profile.resume_path).name,
            }
        finally:
            db.close()

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "interval_hours": self.interval_hours,
            "allowed_intervals": list(ALLOWED_INTERVAL_HOURS),
            "last_updated": self.last_updated,
            "last_filename": self.last_filename,
            "last_status": self.last_status,
            "last_message": self.last_message,
            "browser_running": browser_manager.context is not None,
            "browser_current_url": browser_manager.page.url if browser_manager.page and not browser_manager.page.is_closed() else "",
        }

    async def configure(self, payload: ConfigureIn) -> dict[str, Any]:
        async with self.lock:
            self.interval_hours = payload.interval_hours
            self.last_status = "configured"
            self.last_message = f"Resume refresh interval saved: every {self.interval_hours} hour(s)."
            self._save()
            if self.enabled:
                self.schedule_changed.set()
            return self.status()

    async def start(self) -> dict[str, Any]:
        async with self.lock:
            profile = self._profile_data()
            if not browser_manager.context or not browser_manager.page or browser_manager.page.is_closed():
                raise ValueError("Launch the JobPilot browser and sign in to Naukri first.")
            if "naukri.com" not in (browser_manager.page.url or "").lower():
                raise ValueError("Open your signed-in Naukri profile in the JobPilot browser first.")
            build_structured_resume_name(profile["full_name"], profile["experience_years"], Path(profile["resume_path"]).suffix)
            self.enabled = True
            self.last_status = "scheduled"
            self.last_message = f"Resume refresh enabled. First upload starts now; next upload in {self.interval_hours} hour(s)."
            self._save()
            if self.task is None or self.task.done():
                self.task = asyncio.create_task(self._run_loop(), name="naukri-resume-refresh")
            return self.status()

    async def stop(self) -> dict[str, Any]:
        async with self.lock:
            self.enabled = False
            self.schedule_changed.set()
            self.last_status = "paused"
            self.last_message = "Automatic Naukri resume refresh paused."
            self._save()
            task = self.task
            self.task = None
            if task and task is not asyncio.current_task():
                task.cancel()
            return self.status()

    async def run_now(self) -> dict[str, Any]:
        return await self._apply_resume()

    async def resume_if_enabled(self) -> None:
        if self.enabled and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self._run_loop(), name="naukri-resume-refresh")

    async def shutdown(self) -> None:
        task = self.task
        self.task = None
        if task and task is not asyncio.current_task():
            task.cancel()

    async def _run_loop(self) -> None:
        try:
            while self.enabled:
                # After a backend restart the persistent browser is not restored
                # automatically. Keep the schedule enabled and wait for the user
                # to launch the browser and open Naukri again.
                page = browser_manager.page
                if (not browser_manager.context or page is None or page.is_closed()
                        or "naukri.com" not in (page.url or "").lower()):
                    try:
                        await asyncio.wait_for(self.schedule_changed.wait(), timeout=15)
                    except asyncio.TimeoutError:
                        pass
                    continue
                result = await self._apply_resume()
                if result.get("last_status") == "error":
                    self.enabled = False
                    self._save()
                    break
                self.schedule_changed.clear()
                try:
                    await asyncio.wait_for(self.schedule_changed.wait(), timeout=self.interval_hours * 3600)
                except asyncio.TimeoutError:
                    pass
                if not self.enabled:
                    break
        except asyncio.CancelledError:
            raise
        finally:
            if self.task is asyncio.current_task():
                self.task = None

    async def _apply_resume(self) -> dict[str, Any]:
        async with self.lock:
            try:
                if not browser_manager.context or not browser_manager.page or browser_manager.page.is_closed():
                    raise RuntimeError("Launch the JobPilot browser and sign in to Naukri first.")
                page = browser_manager.page
                if "naukri.com" not in (page.url or "").lower():
                    raise RuntimeError("The active browser tab is not Naukri. Open your signed-in Naukri profile and retry.")
                profile = self._profile_data()
                source = Path(profile["resume_path"])
                filename = build_structured_resume_name(
                    profile["full_name"], profile["experience_years"], source.suffix
                )
                rotation_dir = ROOT / "data" / "resumes" / "naukri_rotation"
                rotation_dir.mkdir(parents=True, exist_ok=True)
                upload_path = rotation_dir / filename
                shutil.copy2(source, upload_path)
                await self._upload_to_naukri(page, upload_path)
                self.last_updated = utc_now()
                self.last_filename = filename
                self.last_status = "success"
                self.last_message = f"Resume uploaded and verified on Naukri as {filename}."
            except Exception as exc:
                self.last_status = "error"
                self.last_message = f"Resume update failed: {type(exc).__name__}: {exc}"
            self._save()
            return self.status()

    async def _upload_to_naukri(self, page: Any, upload_path: Path) -> None:
        if "/mnjuser/profile" not in (page.url or "").lower():
            await page.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(1200)
        body = (await page.locator("body").inner_text(timeout=10000)).lower()
        if "login" in (page.url or "").lower() or ("sign in" in body and "resume" not in body):
            raise RuntimeError("Naukri session is not signed in. Sign in manually and retry.")

        update = page.get_by_text("Update resume", exact=False).last
        await update.wait_for(state="visible", timeout=10000)
        chooser = None
        try:
            async with page.expect_file_chooser(timeout=3500) as chooser_info:
                await update.click(timeout=10000)
            chooser = await chooser_info.value
        except Exception:
            # Some Naukri builds attach a hidden file input without raising a
            # browser file-chooser event. Fall back to the newly available input.
            pass
        if chooser is not None:
            await chooser.set_files(str(upload_path))
        else:
            file_inputs = page.locator('input[type="file"]')
            if await file_inputs.count() == 0:
                raise RuntimeError("Naukri did not expose a resume file input after clicking Update resume.")
            # Prefer an input whose accept attribute permits resume document formats.
            target = None
            for index in range(await file_inputs.count()):
                candidate = file_inputs.nth(index)
                accept = (await candidate.get_attribute("accept") or "").lower()
                if not accept or any(ext in accept for ext in ("pdf", "doc", "rtf")):
                    target = candidate
                    break
            if target is None:
                raise RuntimeError("No suitable PDF/DOC/DOCX resume upload input was found.")
            await target.set_input_files(str(upload_path), timeout=15000)
        await page.wait_for_timeout(1500)

        # Some Naukri versions upload immediately after file selection; others show a modal action.
        uploaded = False
        for scope in [page.locator('[role="dialog"]'), page.locator("body")]:
            for selector in ("button:visible", "[role='button']:visible", "input[type='submit']:visible"):
                controls = scope.locator(selector)
                try:
                    count = await controls.count()
                except Exception:
                    continue
                for index in range(count):
                    control = controls.nth(index)
                    try:
                        label = " ".join([
                            (await control.inner_text()) or "",
                            (await control.get_attribute("aria-label")) or "",
                            (await control.get_attribute("value")) or "",
                        ]).strip().lower()
                        if label in {"upload", "update", "save", "save changes", "upload resume"}:
                            await control.click(timeout=10000)
                            uploaded = True
                            break
                    except Exception:
                        continue
                if uploaded:
                    break
            if uploaded:
                break

        expected = upload_path.name
        # Require observable confirmation; don't claim success merely because a file was selected.
        try:
            await page.get_by_text(expected, exact=False).first.wait_for(state="visible", timeout=20000)
        except Exception as exc:
            raise RuntimeError(
                "Resume file was selected, but Naukri did not display the new filename. "
                "The upload may need a tenant-specific confirmation button; no success was recorded."
            ) from exc


refresher = NaukriResumeRefresher()
router = APIRouter(prefix="/api/naukri-resume-refresh", tags=["Naukri resume refresh"])


@router.get("/status")
async def resume_refresh_status():
    return refresher.status()


@router.put("/configure")
async def configure_resume_refresh(payload: ConfigureIn):
    try:
        return await refresher.configure(payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/start")
async def start_resume_refresh():
    try:
        return await refresher.start()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/stop")
async def stop_resume_refresh():
    return await refresher.stop()


@router.post("/run-now")
async def run_resume_refresh_now():
    return await refresher.run_now()
