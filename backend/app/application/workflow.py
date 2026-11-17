"""ATS workflow helpers for safe, candidate-controlled multi-step navigation."""
import re
from typing import Any
from playwright.async_api import Page

NEXT_LABEL = re.compile(
    r"^(?:next(?:\s+(?:step|page|section|stage))?|"
    r"go\s+to\s+next(?:\s+(?:step|page|section|stage))?|"
    r"continue(?:\s+(?:to\s+)?(?:the\s+)?(?:next|application|review|step|section|stage))?|"
    r"save\s*(?:&|and)\s*(?:go\s+)?(?:continue|next)|save\s+and\s+proceed|"
    r"proceed(?:\s+to\s+(?:the\s+)?(?:next|application|review|step|section|stage))?|"
    r"review(?:\s+application)?)$",
    re.I,
)
BLOCKED_LABEL = re.compile(r"\b(submit|apply now|submit application|finish|complete application|send application)\b", re.I)

WORKFLOW_SCRIPT = r"""() => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const clean = el => [el.innerText, el.getAttribute('aria-label'), el.value, el.title]
    .map(value => (value || '').replace(/\s+/g, ' ').trim()).find(Boolean) || '';
  const errors = [...document.querySelectorAll('[role="alert"], [aria-live="assertive"], .error, .field-error, [data-automation-id*="error"], [data-automation-id*="Error"], [aria-invalid="true"]')]
    .filter(visible).map(clean).filter(Boolean).slice(0,40);
  const active = [...document.querySelectorAll('[aria-current="step"], [aria-selected="true"], [data-automation-id*="progressBar"] [aria-current], [data-automation-id*="step"]')]
    .filter(visible).map(clean).filter(Boolean).slice(0,20);
  const buttons = [...document.querySelectorAll('a, button, [role="button"], input[type="button"], input[type="submit"]')]
    .map((el,domIndex) => ({index:domIndex,label:clean(el),disabled:!!el.disabled || el.getAttribute('aria-disabled')==='true',
      automationId:el.getAttribute('data-automation-id')||'',type:el.getAttribute('type')||'',visible:visible(el)}))
    .filter(x=>x.visible && x.label).slice(0,100);
  const sections = [...document.querySelectorAll('[data-automation-id*="section"], [data-automation-id*="repeat"], [role="group"], fieldset')]
    .filter(visible).map(el=>({label:clean(el).slice(0,140),automationId:el.getAttribute('data-automation-id')||'',
      fields:el.querySelectorAll('input:not([type=hidden]),textarea,select,[role=combobox]').length})).filter(x=>x.fields>0).slice(0,40);
  const progress = [...document.querySelectorAll('[role="progressbar"]')].filter(visible).map(el=>({value:el.getAttribute('aria-valuenow'),max:el.getAttribute('aria-valuemax'),label:el.getAttribute('aria-label')||''}));
  const repeatableActions = buttons.filter(b => /^(add another|add experience|add education|add employment|add language|add skill|add certification|add project)$/i.test(b.label) && !b.disabled);
  const headings = [...document.querySelectorAll('h1,h2,h3,[role="heading"]')].filter(visible).map(clean).filter(Boolean).slice(0,20);
  const controlCount = document.querySelectorAll('input:not([type=hidden]),textarea,select,[role=combobox]').length;
  let hash = 2166136261;
  const formControls = [...document.querySelectorAll('input:not([type=hidden]),textarea,select,[role="combobox"],[role="textbox"],[role="radio"],[role="checkbox"],[contenteditable]:not([contenteditable="false"])')];
  const text = (headings.join('|') + '|' + formControls.map(el => {
    const group = el.closest('fieldset,[role="group"],[data-automation-id*="formField"],[class*="form-field"],[class*="FormField"]') || el.parentElement;
    const labels = el.labels ? [...el.labels].map(label => label.innerText).join(' ') : '';
    return [el.getAttribute('name'),el.id,el.getAttribute('aria-label'),el.getAttribute('placeholder'),labels,group?.innerText].join('|');
  }).join('\n')).replace(/\s+/g,' ').trim().slice(0,12000);
  for (let i=0; i<text.length; i++) { hash ^= text.charCodeAt(i); hash = Math.imul(hash, 16777619); }
  return {title:document.title, url:location.href, errors, activeSteps:active, buttons, sections, progress,
    repeatableActions, headings, controlCount, contentSignature:(hash>>>0).toString(16)};
}"""


async def inspect_workflow(page: Page, ats: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return current-step metadata, validation messages and safe navigation choices."""
    try:
        frames = list(page.frames)
    except Exception:
        frames = []
    if not frames:
        frames = [page]

    pieces = []
    for frame_index, frame in enumerate(frames):
        try:
            piece = await frame.evaluate(WORKFLOW_SCRIPT)
        except Exception:
            continue
        for button in piece.get("buttons", []):
            if frame_index:
                button["frame_index"] = frame_index
        piece["frame_index"] = frame_index
        pieces.append(piece)

    data = {
        "title": pieces[0].get("title", "") if pieces else "",
        "url": page.url,
        "errors": [item for piece in pieces for item in piece.get("errors", [])][:40],
        "activeSteps": [item for piece in pieces for item in piece.get("activeSteps", [])][:20],
        "buttons": [item for piece in pieces for item in piece.get("buttons", [])][:120],
        "sections": [item for piece in pieces for item in piece.get("sections", [])][:40],
        "progress": [item for piece in pieces for item in piece.get("progress", [])][:20],
        "repeatableActions": [item for piece in pieces for item in piece.get("repeatableActions", [])][:40],
        "headings": [item for piece in pieces for item in piece.get("headings", [])][:40],
        "controlCount": sum(piece.get("controlCount", 0) for piece in pieces),
        "contentSignature": "|".join(
            f"{getattr(frames[piece['frame_index']], 'url', '')}:{piece.get('contentSignature', '')}"
            for piece in pieces
        ),
    }
    safe_next = [b for b in data["buttons"] if NEXT_LABEL.match(b["label"]) or
                 (ats and ats.get("key") == "workday" and b.get("automationId") == "bottom-navigation-next")]
    data["next_actions"] = [b for b in safe_next if not b["disabled"] and not BLOCKED_LABEL.search(b["label"])]
    data["ats"] = ats or {"key": "generic", "name": "Generic application form"}
    data["has_validation_errors"] = bool(data["errors"])
    return data


def _step_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return (before.get("url") != after.get("url")
            or before.get("activeSteps") != after.get("activeSteps")
            or before.get("headings") != after.get("headings")
            or before.get("controlCount") != after.get("controlCount")
            or before.get("title") != after.get("title")
            or before.get("errors") != after.get("errors")
            or before.get("contentSignature") != after.get("contentSignature"))


def _structure_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return (before.get("url") != after.get("url")
            or before.get("activeSteps") != after.get("activeSteps")
            or before.get("headings") != after.get("headings")
            or before.get("controlCount") != after.get("controlCount")
            or before.get("progress") != after.get("progress")
            or before.get("title") != after.get("title"))


async def advance_workflow(page: Page, ats: dict[str, Any] | None = None) -> dict[str, Any]:
    """Click only a clearly labelled non-submit Next/Continue control."""
    before = await inspect_workflow(page, ats)
    if before["has_validation_errors"]:
        return {"advanced": False, "reason": "Resolve the visible validation errors before continuing.", "workflow": before}
    if not before["next_actions"]:
        return {"advanced": False, "reason": "No safe Next/Continue control was detected. Continue manually on the page.", "workflow": before}
    candidate = before["next_actions"][0]
    action_selector = 'a, button, [role="button"], input[type="button"], input[type="submit"]'
    try:
        frame_index = candidate.get("frame_index")
        if frame_index is not None and frame_index < len(page.frames):
            root = page.frames[frame_index]
        else:
            root = page
        if candidate.get("automationId") == "bottom-navigation-next":
            locator = root.locator('[data-automation-id="bottom-navigation-next"]')
        else:
            locator = root.locator(action_selector).nth(candidate["index"])
        await locator.click(timeout=8000)
    except Exception as exc:
        # A click can trigger a navigation that detaches its frame; inspect before treating it as failed.
        click_error = str(exc)[:180]
    else:
        click_error = ""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=1800)
    except Exception:
        pass

    after = before
    previous_signature = None
    for _ in range(12):
        await page.wait_for_timeout(250)
        after = await inspect_workflow(page, ats)
        content_changed = after.get("contentSignature") != before.get("contentSignature")
        if (_structure_changed(before, after) or after.get("has_validation_errors")
                or (content_changed and after.get("contentSignature") == previous_signature)):
            break
        previous_signature = after.get("contentSignature")
    changed = not after.get("has_validation_errors") and _step_changed(before, after)
    reason = ("Step transition detected." if changed else
              "The page showed validation errors after clicking; resolve them in the browser." if after.get("has_validation_errors") else
              f"The page did not confirm a step transition. {click_error}".strip())
    return {"advanced": changed, "reason": reason, "workflow": after}
