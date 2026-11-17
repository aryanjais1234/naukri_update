from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from .config import ROOT
from .browser.manager import browser_manager

PROFILE_URL = "https://www.naukri.com/mnjuser/profile"
STATE_PATH = ROOT / "data" / "naukri_headline_rotation.json"
ALLOWED_INTERVAL_MINUTES = (1, 5, 15, 45, 60)
MIN_INTERVAL_SECONDS = 60


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_headlines(items: list[str]) -> list[str]:
    cleaned: list[str] = []
    for item in items:
        value = " ".join((item or "").split()).strip()
        if value and value not in cleaned:
            if len(value) > 250:
                raise ValueError("Each Naukri resume headline must be 250 characters or fewer.")
            cleaned.append(value)
    if len(cleaned) < 3:
        raise ValueError("Add at least 3 distinct resume headline versions.")
    if len(cleaned) > 4:
        raise ValueError("Use no more than 4 headline versions.")
    return cleaned


class ConfigureIn(BaseModel):
    headlines: list[str] = Field(min_length=3, max_length=4)
    interval_minutes: int = Field(default=15)

    @field_validator("interval_minutes")
    @classmethod
    def validate_interval(cls, value: int) -> int:
        if value not in ALLOWED_INTERVAL_MINUTES:
            allowed = ", ".join(str(item) for item in ALLOWED_INTERVAL_MINUTES)
            raise ValueError(f"Choose an update interval of: {allowed} minutes.")
        return value

    @field_validator("headlines")
    @classmethod
    def validate_headlines(cls, value: list[str]) -> list[str]:
        try:
            return normalize_headlines(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


class NaukriHeadlineRotator:
    """Local scheduler for user-authored Naukri resume headline variants."""

    def __init__(self) -> None:
        self.headlines: list[str] = []
        self.interval_minutes = 15
        self.enabled = False
        self.next_index = 0
        self.last_updated = ""
        self.last_headline = ""
        self.last_status = "idle"
        self.last_message = "Configure 3–4 truthful headline versions to begin."
        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.schedule_changed = asyncio.Event()
        self.consecutive_failures = 0
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            self.headlines = normalize_headlines(data.get("headlines", [])) if data.get("headlines") else []
            stored_interval = int(data.get("interval_minutes", 15))
            self.interval_minutes = stored_interval if stored_interval in ALLOWED_INTERVAL_MINUTES else 15
            self.enabled = bool(data.get("enabled", False))
            self.next_index = int(data.get("next_index", 0)) % max(len(self.headlines), 1)
            self.last_updated = str(data.get("last_updated", ""))
            self.last_headline = str(data.get("last_headline", ""))
            self.last_status = str(data.get("last_status", "idle"))
            self.last_message = str(data.get("last_message", ""))
        except Exception:
            self.enabled = False
            self.headlines = []

    def _save(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps({
            "headlines": self.headlines,
            "interval_minutes": self.interval_minutes,
            "enabled": self.enabled,
            "next_index": self.next_index,
            "last_updated": self.last_updated,
            "last_headline": self.last_headline,
            "last_status": self.last_status,
            "last_message": self.last_message,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "interval_minutes": self.interval_minutes,
            "headlines": self.headlines,
            "next_index": self.next_index,
            "next_headline": self.headlines[self.next_index] if self.headlines else "",
            "last_updated": self.last_updated,
            "last_headline": self.last_headline,
            "last_status": self.last_status,
            "last_message": self.last_message,
            "browser_running": browser_manager.context is not None,
            "browser_current_url": browser_manager.page.url if browser_manager.page and not browser_manager.page.is_closed() else "",
        }

    async def configure(self, payload: ConfigureIn) -> dict[str, Any]:
        async with self.lock:
            self.headlines = payload.headlines
            self.interval_minutes = payload.interval_minutes
            self.next_index %= len(self.headlines)
            self.last_message = "Headline versions saved locally."
            self.last_status = "configured"
            self._save()
            if self.enabled:
                # Recalculate the pending wait from the newly selected interval.
                self.schedule_changed.set()
            return self.status()

    async def start(self) -> dict[str, Any]:
        async with self.lock:
            if len(self.headlines) < 3:
                raise ValueError("Save at least 3 headline versions before starting rotation.")
            if not browser_manager.context or not browser_manager.page or browser_manager.page.is_closed():
                raise ValueError("Launch the JobPilot browser and sign in to Naukri first.")
            if "naukri.com" not in (browser_manager.page.url or "").lower():
                raise ValueError("Open your signed-in Naukri profile in the JobPilot browser before starting rotation.")
            self.enabled = True
            self.last_status = "scheduled"
            self.last_message = f"Headline rotation enabled. Next update is scheduled in {self.interval_minutes} minutes."
            self._save()
            if self.task is None or self.task.done():
                self.task = asyncio.create_task(self._run_loop(), name="naukri-headline-rotation")
            return self.status()

    async def stop(self) -> dict[str, Any]:
        async with self.lock:
            self.enabled = False
            self.schedule_changed.set()
            self.last_status = "paused"
            self.last_message = "Automatic headline rotation paused."
            self._save()
            task = self.task
            self.task = None
            if task and task is not asyncio.current_task():
                task.cancel()
            return self.status()

    async def run_now(self) -> dict[str, Any]:
        async with self.lock:
            if len(self.headlines) < 3:
                raise ValueError("Save at least 3 headline versions first.")
        return await self._apply_next()

    async def resume_if_enabled(self) -> None:
        if self.enabled and len(self.headlines) >= 3 and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self._run_loop(), name="naukri-headline-rotation")

    async def shutdown(self) -> None:
        # Stop the in-memory task but preserve enabled=True so the scheduler can
        # resume after a normal backend restart.
        task = self.task
        self.task = None
        if task and task is not asyncio.current_task():
            task.cancel()

    async def _run_loop(self) -> None:
        try:
            while self.enabled:
                # Apply the first version immediately; subsequent versions rotate at the interval.
                result = await self._apply_next()
                # A failed login/profile/selector should pause instead of repeatedly hammering the site.
                if result.get("last_status") == "error":
                    self.consecutive_failures += 1

                    if self.consecutive_failures >= 3:
                        self.enabled = False
                        self._save()
                        break

                    retry_delay = min(
                        60 * (2 ** (self.consecutive_failures - 1)),
                        300
                    )

                    await asyncio.sleep(retry_delay)
                    continue

                self.consecutive_failures = 0
                # Wait for the selected interval, but allow a settings change to
                # restart the countdown without performing an extra immediate update.
                self.schedule_changed.clear()
                try:
                    await asyncio.wait_for(
                        self.schedule_changed.wait(),
                        timeout=self.interval_minutes * 60,
                    )
                    continue
                except asyncio.TimeoutError:
                    pass
                if not self.enabled:
                    break
        except asyncio.CancelledError:
            raise
        finally:
            if self.task is asyncio.current_task():
                self.task = None

    async def _apply_next(self) -> dict[str, Any]:
        async with self.lock:
            if not self.headlines:
                raise ValueError("No headline versions configured.")
            if not browser_manager.context:
                self.last_status = "error"
                self.last_message = "Launch the JobPilot browser and sign in to Naukri first."
                self._save()
                return self.status()
            page = browser_manager.page
            if page is None or page.is_closed():
                self.last_status = "error"
                self.last_message = "No active browser tab. Reopen your Naukri profile."
                self._save()
                return self.status()
            current_url = (page.url or "").lower()
            if "naukri.com" not in current_url:
                self.last_status = "error"
                self.last_message = "The active browser tab is not Naukri. Open your signed-in Naukri profile, then retry."
                self._save()
                return self.status()
            headline = self.headlines[self.next_index]
            try:
                await self._edit_resume_headline(page, headline)
                self.last_headline = headline
                self.last_updated = utc_now()
                self.last_status = "success"
                self.last_message = "Naukri resume headline updated and verified."
                self.next_index = (self.next_index + 1) % len(self.headlines)
            except Exception as exc:
                self.last_status = "error"
                self.last_message = f"Headline update failed: {type(exc).__name__}: {exc}"
            self._save()
            return self.status()

    async def _edit_resume_headline(self, page: Any, headline: str) -> None:
        # If the browser is on another Naukri page, navigate to the profile first.
        if "/mnjuser/profile" not in (page.url or "").lower():
            await page.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(1200)

        body = (await page.locator("body").inner_text(timeout=10000)).lower()
        if "login" in (page.url or "").lower() or ("sign in" in body and "resume headline" not in body):
            raise RuntimeError("Naukri session is not signed in. Sign in manually and retry.")
        # The user's live Naukri DOM exposes a stable accessible name for this
        # exact control. Prefer it over walking ancestors from the Quick Links
        # text (which is a separate navigation item, not the profile card).
        editor = await self._find_headline_editor(page)
        if editor is None:
            edit_control = page.locator('[aria-label="Edit resume headline"]')
            try:
                await edit_control.wait_for(state="visible", timeout=10000)
                await edit_control.click(timeout=8000)
            except Exception as exc:
                raise RuntimeError(
                    "Could not click Naukri\'s exact [aria-label=\"Edit resume headline\"] control. "
                    "Confirm the profile page is fully loaded and signed in."
                ) from exc
            editor = await self._find_headline_editor(page)

        if editor is None:
            raise RuntimeError(
                "Naukri's Resume Headline editor did not expose the expected "
                "#resumeHeadlineTxt textarea or an accessible textbox."
            )

        # The live Naukri DOM supplied by the user identifies the editor as
        # textarea#resumeHeadlineTxt[aria-label='Resume headline'].
        await editor.fill(headline)
        filled_value = await editor.input_value()
        if filled_value.strip() != headline.strip():
            raise RuntimeError("The Resume Headline textarea did not retain the requested text.")

        # Naukri's observed modal uses <button class="btn-dark-ot" type="submit">Save</button>.
        # Prefer this exact modal control, then fall back to an exact accessible Save name.
        save_clicked = False
        for frame in page.frames:
            exact_save = frame.locator('button.btn-dark-ot[type="submit"]')
            try:
                if await exact_save.count() and await exact_save.last.is_visible():
                    await exact_save.last.click(timeout=10000)
                    save_clicked = True
                    break
            except Exception:
                pass
            for selector in ("button:visible", "[role='button']:visible", "input[type='submit']:visible"):
                candidates = frame.locator(selector)
                try:
                    count = await candidates.count()
                except Exception:
                    continue
                for index in range(count):
                    candidate = candidates.nth(index)
                    try:
                        label = " ".join([
                            (await candidate.get_attribute("aria-label")) or "",
                            (await candidate.inner_text()) or "",
                            (await candidate.get_attribute("value")) or "",
                        ]).strip().lower()
                        if label == "save":
                            await candidate.click(timeout=10000)
                            save_clicked = True
                            break
                    except Exception:
                        continue
                if save_clicked:
                    break
            if save_clicked:
                break
        if not save_clicked:
            raise RuntimeError(
                "Headline was filled, but Naukri's Save button "
                "button.btn-dark-ot[type='submit'] was not found. The form was not submitted."
            )

        # Verify both that the editor closed and that the saved headline is
        # visible in the profile content. Do not advance the rotation on failure.
        try:
            await page.locator("#resumeHeadlineTxt").wait_for(state="hidden", timeout=12000)
        except Exception:
            # Some page variants remove the modal node rather than hiding it.
            try:
                await page.locator("#resumeHeadlineTxt").wait_for(state="detached", timeout=3000)
            except Exception:
                pass
        try:
            await page.get_by_text(headline, exact=True).first.wait_for(state="visible", timeout=12000)
        except Exception as exc:
            raise RuntimeError(
                "Naukri Save was clicked, but the new headline was not visible on the profile. "
                "The rotation index was not advanced."
            ) from exc

    async def _find_headline_editor(self, page):
        selectors = (
            "textarea#resumeHeadlineTxt",
            'textarea[name="resumeHeadline"][aria-label="Resume headline"]',
            'textarea[aria-label="Resume headline"]',
            'input[aria-label="Resume headline"]',
            '[contenteditable="true"][aria-label="Resume headline"]',
            '[role="textbox"][aria-label="Resume headline"]',
            "textarea",
            'input:not([type="hidden"]):not([type="submit"]):not([type="button"])',
            '[contenteditable="true"]',
            '[role="textbox"]',
        )

        for frame in page.frames:
            for selector_index, selector in enumerate(selectors):
                locator = frame.locator(selector)

                try:
                    count = await locator.count()
                except Exception:
                    continue

                for index in range(count):
                    candidate = locator.nth(index)

                    try:
                        if not await candidate.is_visible():
                            continue

                        box = await candidate.bounding_box()

                        if not box or box["width"] < 100 or box["height"] < 20:
                            continue

                        identity = " ".join([
                            await candidate.get_attribute("id") or "",
                            await candidate.get_attribute("name") or "",
                            await candidate.get_attribute("aria-label") or "",
                            await candidate.get_attribute("placeholder") or "",
                        ]).lower()

                        if selector_index <= 5:
                            return candidate

                        if any(x in identity for x in (
                            "resumeheadline",
                            "resume headline"
                        )):
                            return candidate

                        contextual = await candidate.evaluate("""el => {
                            let node = el;

                            for (let i = 0; node && i < 8; i++) {
                                const text = (
                                    node.innerText ||
                                    node.textContent ||
                                    ""
                                ).toLowerCase();

                                if (text.includes("resume headline")) {
                                    return true;
                                }

                                node = node.parentElement;
                            }

                            return false;
                        }""")

                        if contextual:
                            return candidate

                    except Exception:
                        continue

        return None

rotator = NaukriHeadlineRotator()
router = APIRouter(prefix="/api/naukri-refresh", tags=["Naukri profile refresh"])


@router.get("/status")
async def refresh_status():
    return rotator.status()


@router.put("/configure")
async def configure_refresh(payload: ConfigureIn):
    try:
        return await rotator.configure(payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/start")
async def start_refresh():
    try:
        return await rotator.start()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/stop")
async def stop_refresh():
    return await rotator.stop()


@router.post("/run-now")
async def run_refresh_now():
    try:
        return await rotator.run_now()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
