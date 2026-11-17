import json
import re
import hashlib
from typing import Any
from playwright.async_api import Page

SENSITIVE_OR_DECLARATION = re.compile(
    r"\b(consent|agree|terms|privacy|certify|attest|signature|"
    r"background check|criminal|disability|gender|race|ethnicity|veteran|"
    r"ethnic|religion|sexual orientation|accommodation|nationality|citizenship|"
    r"date of birth|birth date|marital status|age|medical condition|health condition|"
    r"equal opportunity|eeo|affirmative action|self[- ]identify|legal declaration)\b", re.I
)
EXPLICIT_ELIGIBILITY = re.compile(
    r"\b(work authori[sz]ation|authori[sz]ed to work|allowed to work|right to work|employment authori[sz]ation|"
    r"legally eligible to work|eligible to work|eligible for employment|employment eligibility|visa sponsorship|"
    r"require sponsorship|sponsorship required|need sponsorship|sponsorship)\b", re.I
)
WORK_AUTH_TOPIC = re.compile(
    r"\b(work authori[sz]ation|authori[sz]ed to work|allowed to work|right to work|employment authori[sz]ation|"
    r"legally eligible to work|eligible to work|eligible for employment|employment eligibility)\b", re.I
)
SPONSORSHIP_TOPIC = re.compile(r"\b(visa sponsorship|require sponsorship|need sponsorship|sponsorship)\b", re.I)
PLACEHOLDER_VALUES = {"select", "select...", "select one", "please select", "choose", "choose...",
                      "choose one", "--select--", "- select -", "none selected", "no selection",
                      "not selected", "please choose", "--", "-"}

# These declarations must always remain a deliberate human action. Demographic fields
# may be reused only when the candidate explicitly saved that exact answer.
ALWAYS_MANUAL = re.compile(
    r"\b(consent|agree|terms|privacy|certify|attest|signature|background check|criminal|"
    r"equal opportunity|eeo|affirmative action|self[- ]identify|legal declaration|"
    r"accommodation request|reasonable accommodation)\b", re.I
)
DEMOGRAPHIC_TOPIC = re.compile(
    r"\b(gender|date of birth|birth date|country\s*/\s*territory\s+of birth|country of birth|territory of birth|region of birth|"
    r"city of birth|marital status|citizenship status|primary nationality|nationality|"
    r"race|ethnicity|veteran|religion|sexual orientation|disability)\b", re.I
)

def _normalize_question(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
    return re.sub(r"\s+", " ", value).strip()

def find_saved_preference(field: dict[str, Any], preferences: dict[str, Any]):
    """Find a candidate-authored Q&A whose distinctive question matches this field."""
    prompt = " ".join(str(field.get(k) or "") for k in ("label", "group_label", "name") if field.get(k))
    prompt_norm = _normalize_question(prompt)
    if not prompt_norm:
        return None, None
    prompt_tokens = {t for t in prompt_norm.split() if len(t) > 2}
    best = None
    best_score = 0.0
    for question, answer in preferences.items():
        q_norm = _normalize_question(question)
        if not q_norm or answer in (None, ""):
            continue
        q_tokens = {t for t in q_norm.split() if len(t) > 2}
        if q_norm in prompt_norm or prompt_norm in q_norm:
            return question, answer
        if not q_tokens or not prompt_tokens:
            continue
        score = len(q_tokens & prompt_tokens) / len(q_tokens | prompt_tokens)
        if score > best_score:
            best, best_score = (question, answer), score
    return best if best_score >= 0.65 else (None, None)


def is_field_answered(field: dict[str, Any]) -> bool:
    """Conservatively tell whether a form field has an actual user-selected answer."""
    kind = str(field.get("type") or "").lower()
    if kind == "checkbox":
        return bool(field.get("checked"))
    if kind == "radio":
        return bool(field.get("checked"))
    value = str(field.get("value") or "").strip()
    if not value:
        return False
    if kind in {"select", "combobox"}:
        label = str(field.get("selected_label") or field.get("placeholder") or "").strip().lower()
        normalized_label = re.sub(r"^[\s\-–—*]+", "", label)
        if (label in PLACEHOLDER_VALUES or value.lower() in PLACEHOLDER_VALUES
                or re.match(r"^(please\s+)?(select|choose)\b", normalized_label)):
            return False
    return True
SKIP_TYPES = {"hidden", "submit", "button", "reset", "image", "password"}

async def inspect_page(page: Page) -> dict[str, Any]:
    """Inspect visible native and ARIA controls in the page and embedded application frames."""
    native_selector = "input, textarea, select"
    native_script = r"""els => {
      const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
      const clean = value => (value || '').replace(/\s+/g, ' ').replace(/\s*\*\s*/g, ' ').trim();
      const controls = 'input, textarea, select, [role="combobox"], [role="checkbox"], [role="radio"], [role="textbox"]';
      const labelFor = el => {
        if (el.labels && el.labels.length) return clean([...el.labels].map(x => x.innerText).join(' '));
        const wrap = el.closest('label'); if (wrap) return clean(wrap.innerText);
        const aria = el.getAttribute('aria-label'); if (aria) return clean(aria);
        const labelled = el.getAttribute('aria-labelledby');
        if (labelled) { const text = clean(labelled.split(/\s+/).map(id => document.getElementById(id)?.innerText || '').join(' ')); if (text) return text; }
        // Many ATS forms render a visual label immediately before the control without a for/id link.
        let ancestor = el.parentElement;
        for (let depth = 0; ancestor && depth < 5; depth++, ancestor = ancestor.parentElement) {
          if (ancestor.querySelectorAll(controls).length > 1) continue;
          let branch = el;
          while (branch.parentElement && branch.parentElement !== ancestor) branch = branch.parentElement;
          const children = [...ancestor.children];
          const position = children.indexOf(branch);
          const preceding = children.slice(0, position < 0 ? children.length : position).reverse()
            .map(node => clean(node.innerText || node.textContent))
            .find(text => text && text.length <= 280 && !/^(required|optional)$/i.test(text));
          if (preceding) return preceding;
          const node = ancestor.querySelector('legend, .field-label, .form-label, [class*="label"], [data-automation-id*="label"], p, h3, h4');
          const text = clean(node?.innerText); if (text && text.length <= 280) return text;
        }
        return '';
      };
      return els.map((el, index) => {
        const role = el.getAttribute('role') || '';
        const type = el.tagName.toLowerCase() === 'select' ? 'select' : (role === 'combobox' ? 'combobox' : (el.type || el.tagName.toLowerCase()).toLowerCase());
        let group = el.closest('fieldset, [role=group], [data-automation-id="formField"], [data-automation-id="formFieldContainer"], [class*="form-field"], [class*="FormField"]');
        if (!group) { let node=el.parentElement; for(let i=0;node&&i<4;i++,node=node.parentElement) { if(node.querySelectorAll(controls).length===1){group=node;break;} } }
        const parentText = clean(group?.innerText || el.parentElement?.innerText || '');
        const groupId = group?.getAttribute('data-automation-id') || group?.id || '';
        const groupLabel = clean(group?.querySelector('legend, h1, h2, h3, .field-label, .form-label, [data-automation-id*="label"]')?.innerText || '');
        const label = labelFor(el) || groupLabel;
        const describedBy = el.getAttribute('aria-describedby');
        const description = describedBy ? clean(describedBy.split(/\s+/).map(id => document.getElementById(id)?.innerText || '').join(' ')) : '';
        const options = el.tagName === 'SELECT' ? [...el.options].map(o => ({label:clean(o.textContent), value:o.value, disabled:o.disabled})) : [];
        const selectedLabel = el.tagName === 'SELECT' && el.selectedIndex >= 0 ? clean(el.options[el.selectedIndex]?.textContent) : '';
        return {index, selector:'input, textarea, select', tag:el.tagName.toLowerCase(), type, id:el.id || '', name:el.name || '',
          label, context:(parentText || description || '').slice(0,900), group_id:groupId, group_label:groupLabel, placeholder:el.getAttribute('placeholder') || '',
          required:!!el.required || el.getAttribute('aria-required') === 'true' || /required/i.test(parentText),
          value:type==='password'?'':(el.value||''), selected_label:selectedLabel,
          checked:!!el.checked, disabled:!!el.disabled, visible:visible(el), options};
      });
    }"""
    custom_selector = '[role="combobox"], [role="checkbox"], [role="radio"], [role="textbox"], [role="searchbox"], [contenteditable]:not([contenteditable="false"]), button[aria-haspopup="listbox"], [data-automation-id="selectWidget"], [data-automation-id="checkbox"], [data-automation-id="radio"], [data-automation-id="multiSelectContainer"]'
    custom_script = r"""els => {
      const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
      const clean = value => (value || '').replace(/\s+/g, ' ').trim();
      return els.map((el,index)=>({el,index})).filter(item => !item.el.matches('input, textarea, select')).map(({el,index})=>{
        const automation=el.getAttribute('data-automation-id')||'';
        const role=el.getAttribute('role')||(automation==='checkbox'?'checkbox':automation==='radio'?'radio':(automation==='selectWidget'||automation==='multiSelectContainer'||el.tagName.toLowerCase()==='button')?'combobox':el.isContentEditable?'textbox':'');
        const group=el.closest('[role="group"], fieldset, [data-automation-id*="formField"], [data-automation-id*="section"]')||el.parentElement;
        const parent=clean(group?.innerText||'');
        const groupLabel=clean(group?.querySelector('legend,h1,h2,h3,label,[class*="label"],[data-automation-id*="label"]')?.innerText||'');
        const labelled=el.getAttribute('aria-labelledby')?.split(/\s+/).map(id=>document.getElementById(id)?.innerText||'').join(' ')||'';
        const label=clean(el.getAttribute('aria-label')||labelled||groupLabel);
        const options=[...document.querySelectorAll('[role="option"],[data-automation-id="promptOption"]')].filter(visible)
          .map(o=>({label:clean(o.innerText),value:o.getAttribute('data-value')||o.getAttribute('value')||'',disabled:o.getAttribute('aria-disabled')==='true'})).filter(o=>o.label).slice(0,80);
        return {index,selector:'[role=\"combobox\"], [role=\"checkbox\"], [role=\"radio\"], [role=\"textbox\"], [role=\"searchbox\"], [contenteditable]:not([contenteditable=\"false\"]), button[aria-haspopup=\"listbox\"], [data-automation-id=\"selectWidget\"], [data-automation-id=\"checkbox\"], [data-automation-id=\"radio\"], [data-automation-id=\"multiSelectContainer\"]',tag:el.tagName.toLowerCase(),type:role,id:el.id||'',name:el.getAttribute('name')||'',label,context:parent.slice(0,900),
          placeholder:el.getAttribute('placeholder')||'',group_id:group?.getAttribute('data-automation-id')||group?.id||'',group_label:groupLabel,
          required:el.getAttribute('aria-required')==='true'||/required/i.test(parent),value:el.getAttribute('data-value')||el.getAttribute('value')||el.innerText?.trim()||'',
          checked:el.getAttribute('aria-checked')==='true',disabled:el.getAttribute('aria-disabled')==='true',visible:visible(el),options:role==='combobox'?options:[]};
      });
    }"""
    frames = []
    try:
        frames = list(page.frames)
    except Exception:
        pass
    if not frames:
        frames = [page]
    fields = []
    for frame_index, frame in enumerate(frames):
        try:
            frame_fields = await frame.locator(native_selector).evaluate_all(native_script)
            custom = await frame.locator(custom_selector).evaluate_all(custom_script)
        except Exception:
            # Third-party widgets can remove an iframe while the page is being inspected.
            continue
        frame_url = getattr(frame, "url", "") or ""
        frame_name = getattr(frame, "name", "") or ""
        for field in [*frame_fields, *custom]:
            if frame_index:
                field["frame_index"] = frame_index
                field["frame_url"] = frame_url
                field["frame_name"] = frame_name
            fields.append(field)
    normalized=[]
    for f in fields:
        if (not f['visible'] and f['type']!='file') or f['disabled'] or f['type'] in SKIP_TYPES: continue
        identity='|'.join(str(f.get(k,'')) for k in ('frame_index','frame_url','frame_name','tag','type','name','id','label','index'))
        digest=hashlib.sha1(identity.encode('utf-8')).hexdigest()[:10]
        f['key']=f'{f["tag"]}:{f["name"] or f["id"] or f["index"]}:{f["index"]}:{digest}'
        f['needs_human_review']=bool(SENSITIVE_OR_DECLARATION.search(' '.join([f['label'],f.get('group_label',''),f['context'],f['name']])))
        normalized.append(f)
    return {'url':page.url,'title':await page.title(),'fields':normalized,'field_count':len(normalized)}

def parse_preferences(raw: str) -> dict:
    try:
        value=json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}

def suggest_profile_value(field: dict, profile: dict, preferences: dict):
    """Conservative exact/semantic mapping. Unknown questions are left blank."""
    label=" ".join([field.get("label",""), field.get("context",""), field.get("name","")]).lower()
    if field.get("needs_human_review") or SENSITIVE_OR_DECLARATION.search(label):
        return None, "Sensitive, legal or consent question requires your decision."
    # User-authored answers for employer-specific questions take precedence.
    for question, answer in preferences.items():
        if question.lower().strip() and question.lower().strip() in label:
            return answer, "Matched an explicit saved application preference."
    mappings = [
        (("first name", "given name"), profile.get("full_name","").split(" ")[0] if profile.get("full_name") else ""),
        (("last name", "family name", "surname"), profile.get("full_name","").split(" ")[-1] if profile.get("full_name") else ""),
        (("full name", "legal name", "your name"), profile.get("full_name","")),
        (("email",), profile.get("email","")),
        (("phone", "mobile", "telephone"), profile.get("phone","")),
        (("current location", "city", "address city"), profile.get("location","")),
        (("current employer", "company name"), profile.get("current_employer","")),
        (("current job title", "current role", "job title"), profile.get("current_role","")),
        (("notice period", "when can you start"), profile.get("notice_period","")),
        (("years of experience", "total experience"), profile.get("experience_years","")),
    ]
    for terms, value in mappings:
        if value and any(term in label for term in terms):
            return value, "Matched candidate profile."
    # Skills and free-text preference answers should not be guessed.
    return None, "No reliable profile value or explicit preference found."


def _locator_root_for_field(page: Page, field: dict[str, Any]):
    """Resolve the frame that owned a field when it was inspected."""
    frame_index = field.get("frame_index")
    if frame_index is None:
        return page
    try:
        frames = list(page.frames)
    except Exception:
        frames = []
    expected_url = str(field.get("frame_url") or "")
    expected_name = str(field.get("frame_name") or "")
    index = int(frame_index)
    if 0 <= index < len(frames):
        candidate = frames[index]
        if ((not expected_url or candidate.url == expected_url)
                and (not expected_name or candidate.name == expected_name)):
            return candidate
    for candidate in frames[1:]:
        if expected_name and candidate.name == expected_name:
            return candidate
        if expected_url and candidate.url == expected_url:
            return candidate
    raise ValueError("The embedded application frame changed. Re-inspect the page and try again.")


def _css_attribute_value(value: str) -> str:
    """Quote an HTML attribute value for a CSS selector."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\a ").replace("\r", "\\d ")
    return f'"{escaped}"'

async def prepare_fill(page: Page, inspected: dict, profile: dict, answers: dict | None = None):
    """Fill only reviewed non-sensitive controls. Never clicks submit."""
    answers = answers or {}
    preferences = parse_preferences(profile.get("application_preferences", "{}"))
    report=[]
    for field in inspected.get("fields", []):
        key=field["key"]
        if key not in answers and is_field_answered(field):
            report.append({"key": key, "label": field.get("label") or field.get("name") or key,
                           "status": "already_filled", "reason": "This field already contains a selected or entered value."})
            continue
        if key not in answers:
            value, reason = None, "No answer was supported by the saved profile, resume or Q&A preferences."
        else:
            value, reason = answers[key], "Mapped from candidate-provided evidence by OpenAI."
        if value is None or value == "":
            report.append({"key":key,"label":field.get("label") or field.get("name") or key,
                           "status":"needs_input","reason":reason})
            continue
        saved_question, saved_value = find_saved_preference(field, preferences)
        prompt_text = " ".join(str(field.get(k) or "") for k in ("label", "group_label", "name"))
        if ALWAYS_MANUAL.search(prompt_text):
            report.append({"key":key,"label":field.get("label") or key,"status":"blocked",
                           "reason":"Consent, legal declaration or attestation requires your direct action."})
            continue
        if field.get("needs_human_review") and not (saved_question and str(saved_value) == str(value)):
            report.append({"key":key,"label":field.get("label") or key,"status":"blocked",
                           "reason":"Sensitive field can only use an exact answer you explicitly saved in reusable preferences."})
            continue
        if field.get("type") in {"checkbox","radio"} and not isinstance(value, (bool, str, int)) and not (field.get("type") == "checkbox" and isinstance(value, list)):
            report.append({"key":key,"status":"needs_input","reason":"Invalid selection format."})
            continue
        try:
            locator_root = _locator_root_for_field(page, field)
            if field.get("id"):
                locator = locator_root.locator(f'[id={_css_attribute_value(field["id"])}]')
            else:
                locator=locator_root.locator(field.get("selector", "input, textarea, select")).nth(field["index"])
            if field["type"] in {"checkbox","radio"}:
                # Convert explicit preference values deliberately; bool("No") is True in Python.
                truthy = {"yes", "true", "1", "checked", "on"}
                falsy = {"no", "false", "0", "unchecked", "off", ""}
                if field["type"] == "checkbox":
                    if field.get("selector", "").startswith("[role="):
                        if isinstance(value, list):
                            selected_values = {str(v).strip().lower() for v in value}
                            option_value = str(field.get("value", "")).strip().lower() or str(field.get("label", "")).strip().lower()
                            should_check = option_value in selected_values
                        elif isinstance(value, bool): should_check = value
                        else:
                            normalized = str(value).strip().lower()
                            option_tokens = {str(field.get("value", "")).strip().lower(),
                                             str(field.get("label", "")).strip().lower()} - {""}
                            if normalized in truthy | falsy:
                                should_check = normalized in truthy
                            elif normalized in option_tokens:
                                should_check = True
                            else:
                                try: decoded = json.loads(str(value))
                                except Exception: decoded = None
                                if isinstance(decoded, list):
                                    selected_values = {str(v).strip().lower() for v in decoded}
                                    should_check = bool(option_tokens & selected_values)
                                else:
                                    report.append({"key":key,"label":field.get("label") or key,"status":"needs_input",
                                                   "reason":"Custom checkbox answer must be explicit Yes/No or match this option."})
                                    continue
                        if bool(field.get("checked")) != should_check:
                            await locator.click()
                    elif isinstance(value, list):
                        should_check = (str(field.get("value", "")).strip().lower() or str(field.get("label", "")).strip().lower()) in {str(v).strip().lower() for v in value}
                    elif isinstance(value, bool):
                        should_check = value
                    else:
                        normalized = str(value).strip()
                        normalized_lower = normalized.lower()
                        if normalized_lower in truthy | falsy:
                            should_check = normalized_lower in truthy
                        else:
                            # A checkbox in a multi-option group is identified by its own
                            # option label/value. This allows OpenAI to select exact options.
                            option_tokens = {
                                str(field.get("value", "")).strip().lower(),
                                str(field.get("label", "")).strip().lower(),
                            } - {""}
                            if normalized_lower in option_tokens:
                                should_check = True
                            else:
                                try:
                                    decoded = json.loads(normalized)
                                except Exception:
                                    decoded = None
                                if isinstance(decoded, list):
                                    selected_values = {str(v).strip().lower() for v in decoded}
                                    should_check = bool(option_tokens & selected_values)
                                else:
                                    report.append({"key":key,"label":field.get("label") or key,"status":"needs_input",
                                                   "reason":"Checkbox answer must be explicit Yes/No or match this option."})
                                    continue
                    if not field.get("selector", "").startswith("[role="):
                        await locator.set_checked(should_check)
                else:
                    # A radio is selected only when its own option matches the explicit answer.
                    option_values = {str(field.get("value", "")).strip().lower(),
                                     str(field.get("label", "")).strip().lower()}
                    if isinstance(value, bool):
                        matches = (str(field.get("value", "")).strip().lower() in (truthy if value else falsy))
                    else:
                        matches = str(value).strip().lower() in option_values
                    if matches:
                        if field.get("selector", "").startswith("[role="):
                            await locator.click()
                        else:
                            await locator.check()
                    else:
                        report.append({"key":key,"label":field.get("label") or key,"status":"skipped",
                                       "reason":"This radio option does not match the candidate's explicit answer."})
                        continue
            elif field["tag"] == "select":
                if isinstance(value, dict):
                    if "label" in value: await locator.select_option(label=str(value["label"]))
                    else: await locator.select_option(value=str(value.get("value","")))
                else:
                    try: await locator.select_option(label=str(value))
                    except Exception: await locator.select_option(value=str(value))
            elif field.get("type") == "combobox":
                values = value if isinstance(value, list) else [value]
                for item in values:
                    option_label = item.get("label") if isinstance(item, dict) else str(item)
                    await locator.click()
                    # Exact accessible option matching avoids selecting a similarly named value.
                    option = page.get_by_role("option", name=option_label, exact=True)
                    if await option.count() == 0:
                        option = page.locator('[role="option"], [data-automation-id="promptOption"]').filter(has_text=re.compile(r"^" + re.escape(option_label) + r"$", re.I))
                    await option.first.click(timeout=5000)
            elif field["type"] == "file":
                # Resume upload is only allowed when a candidate explicitly saved a local path.
                resume=profile.get("resume_path","")
                if not resume:
                    raise ValueError("No resume path is saved in your candidate profile.")
                await locator.set_input_files(resume)
            else:
                await locator.fill(str(value))
            report.append({"key":key,"label":field.get("label") or field.get("name") or key,
                           "status":"filled","value":value,"reason":reason})
        except Exception as exc:
            report.append({"key":key,"label":field.get("label") or key,"status":"error",
                           "reason":str(exc)[:240]})
    return {"url":page.url,"results":report,
            "filled":sum(x["status"]=="filled" for x in report),
            "already_filled":sum(x["status"]=="already_filled" for x in report),
            "needs_input":sum(x["status"] in {"needs_input","blocked","error"} for x in report),
            "submission_performed":False}
