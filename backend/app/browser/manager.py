import asyncio
from pathlib import Path
from playwright.async_api import async_playwright
from ..config import settings

class BrowserManager:
    def __init__(self):
        self.playwright = None
        self.context = None
        self.page = None
        self.lock = asyncio.Lock()

    async def start(self):
        async with self.lock:
            if self.context:
                return self.status()
            Path(settings.browser_profile_dir).mkdir(parents=True, exist_ok=True)
            self.playwright = await async_playwright().start()
            self.context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir=settings.browser_profile_dir,
                headless=False,
                args=[],
            )
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            self.context.on("page", self._on_new_page)
            if self.page:
                self._bind_page(self.page)
            return self.status()

    def _bind_page(self, page):
        # Track navigations on a page so the agent follows newly opened employer tabs.
        if getattr(page, "_jobpilot_navigation_bound", False):
            return
        setattr(page, "_jobpilot_navigation_bound", True)
        page.on("framenavigated", lambda frame, target=page: self._on_frame_navigated(target, frame))

    def _on_frame_navigated(self, page, frame):
        if frame == page.main_frame:
            self.page = page

    def _on_new_page(self, page):
        # Track newly opened tabs/popups so the agent resumes on the page the user just opened.
        self.page = page
        self._bind_page(page)


    async def open_searches(self, searches: list[dict]):
        """Open only the platform search pages explicitly selected by the user.

        The first selected platform reuses the current tab. Additional selected
        platforms open in separate tabs. No third-party/public job feeds are queried.
        """
        if not searches:
            raise ValueError("Select at least one job platform.")
        if not self.context:
            await self.start()

        opened = []
        first_page = None
        for index, search in enumerate(searches):
            url = search.get("url", "")
            if not url.startswith(("https://", "http://")):
                opened.append({"platform": search.get("platform", "Unknown"),
                               "url": url, "status": "error",
                               "error": "Only http:// and https:// URLs are allowed."})
                continue
            try:
                if index == 0 and self.page is not None and not self.page.is_closed():
                    page = self.page
                else:
                    page = await self.context.new_page()
                    self._bind_page(page)
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                if first_page is None:
                    first_page = page
                opened.append({"platform": search.get("platform", "Unknown"),
                               "url": page.url, "title": await page.title(),
                               "status": "opened"})
            except Exception as exc:
                opened.append({"platform": search.get("platform", "Unknown"),
                               "url": url, "status": "error",
                               "error": f"{type(exc).__name__}: {exc}"})

        # Keep the first selected platform as the agent's active page.
        if first_page is not None:
            self.page = first_page
            try:
                await first_page.bring_to_front()
            except Exception:
                pass
        return opened

    async def stop(self):
        async with self.lock:
            if self.context:
                await self.context.close()
                self.context = None
            if self.playwright:
                await self.playwright.stop()
                self.playwright = None
            self.page = None
            return self.status()

    async def open(self, url: str):
        if not self.context:
            await self.start()
        if not url.startswith(("https://", "http://")):
            raise ValueError("Only http:// and https:// URLs are allowed.")
        if self.page is None or self.page.is_closed():
            self.page = await self.context.new_page()
            self._bind_page(self.page)
        await self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        return {"url": self.page.url, "title": await self.page.title()}

    def status(self):
        return {"running": self.context is not None,
                "current_url": self.page.url if self.page else None}

browser_manager = BrowserManager()
