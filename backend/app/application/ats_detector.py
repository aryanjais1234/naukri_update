from urllib.parse import urlparse

ATS_SIGNATURES = [
    ("workday", "Workday", ("myworkdayjobs.com", "workday.com")),
    ("eightfold", "Eightfold", ("eightfold.ai", "eightfold.com")),
    ("greenhouse", "Greenhouse", ("greenhouse.io", "boards.greenhouse.io")),
    ("lever", "Lever", ("lever.co", "jobs.lever.co")),
    ("avature", "Avature", ("avature.net", "avature.com")),
    ("smartrecruiters", "SmartRecruiters", ("smartrecruiters.com",)),
    ("ashby", "Ashby", ("ashbyhq.com",)),
    ("icims", "iCIMS", ("icims.com",)),
    ("successfactors", "SAP SuccessFactors", ("successfactors.com",)),
    ("taleo", "Oracle Taleo", ("taleo.net",)),
]

async def detect_ats(page):
    url = page.url or ""
    host = (urlparse(url).hostname or "").lower()
    try:
        frame_urls = [frame.url for frame in page.frames]
    except Exception:
        frame_urls = []
    hosts = [host, *((urlparse(frame_url).hostname or "").lower() for frame_url in frame_urls)]
    title = ""
    try:
        title = (await page.title()).lower()
    except Exception:
        pass
    body_parts = []
    asset_parts = []
    try:
        frames = list(page.frames)
    except Exception:
        frames = [page]
    if not frames:
        frames = [page]
    for frame in frames:
        try:
            body_parts.append((await frame.locator("body").inner_text(timeout=1200)).lower()[:6000])
        except Exception:
            pass
        try:
            asset_parts.append(await frame.locator("script[src], link[href]").evaluate_all(
                "els => els.map(el => el.src || el.href || '').join(' ').toLowerCase()"
            ))
        except Exception:
            pass
    body = " ".join(body_parts)[:16000]
    assets = " ".join(asset_parts)[:6000]
    for key, label, domains in ATS_SIGNATURES:
        if any(domain in frame_host for frame_host in hosts for domain in domains):
            return {"key": key, "name": label, "adapter": key if key in {"workday", "eightfold"} else "generic",
                    "confidence": "high", "detected_by": "hostname"}
    # DOM/title signatures are hints only; tenant branding can cause false positives.
    signatures = {
        "workday": ("workday", "wd5.myworkdayjobs"),
        "eightfold": ("eightfold", "talent community"),
        "greenhouse": ("greenhouse", "gh_jid"),
        "lever": ("lever", "lever jobs"),
        "avature": ("avature",),
    }
    combined = f"{title} {body} {assets}"
    for key, tokens in signatures.items():
        hits = sum(token in combined for token in tokens)
        if hits:
            label = next((x[1] for x in ATS_SIGNATURES if x[0] == key), key.title())
            return {"key": key, "name": label, "adapter": key if key in {"workday", "eightfold"} else "generic",
                    "confidence": "medium", "detected_by": "page_content"}
    return {"key": "generic", "name": "Generic application form", "adapter": "generic",
            "confidence": "low", "detected_by": "fallback"}
