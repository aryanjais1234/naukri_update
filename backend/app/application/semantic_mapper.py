import json
import httpx
from ..config import settings
from .form_engine import parse_preferences

ALLOWED_PROFILE_KEYS = {
    "full_name", "email", "phone", "location", "experience_years",
    "current_employer", "current_role", "skills", "preferred_roles",
    "preferred_locations", "expected_compensation", "notice_period"
}

async def semantic_suggestions(fields: list[dict], profile: dict) -> dict:
    """Ask local Ollama to map form questions to existing candidate facts.

    The model may select a source key, but it may not invent an answer.
    Backend validates every source key and returns the stored value verbatim.
    """
    preferences = parse_preferences(profile.get("application_preferences", "{}"))
    sources = {key: profile.get(key) for key in ALLOWED_PROFILE_KEYS if profile.get(key) not in (None, "")}
    sources.update({f"preference:{key}": value for key, value in preferences.items() if value not in (None, "")})
    safe_fields = [
        {"key": f.get("key"), "label": f.get("label"), "name": f.get("name"),
         "context": f.get("context"), "type": f.get("type"), "required": f.get("required"),
         "options": [o.get("label") for o in f.get("options", [])][:40],
         "needs_human_review": f.get("needs_human_review", False)}
        for f in fields if not f.get("needs_human_review")
    ]
    if not safe_fields:
        return {"suggestions": [], "model": settings.ollama_model, "mode": "no_eligible_fields"}
    prompt = {
        "task": "Map application questions to existing candidate data. Do not invent or infer facts.",
        "rules": [
            "Return JSON only: {suggestions:[{key,source_key,confidence,reason}]}",
            "source_key must exactly match one key in candidate_sources; otherwise use null.",
            "Do not answer legal, consent, demographic, protected-characteristic, background-check or declaration questions.",
            "Do not infer sponsorship, work authorization, relocation, salary, notice period or eligibility unless an explicit saved source exists.",
            "For dropdown/radio fields, only suggest a source if its value can be matched to one of the listed options.",
            "Confidence must be a number from 0 to 1. Prefer null when uncertain."
        ],
        "candidate_sources": sources,
        "application_fields": safe_fields,
    }
    try:
        async with httpx.AsyncClient(timeout=50) as client:
            response = await client.post(
                f"{settings.ollama_base_url.rstrip('/')}/api/chat",
                json={"model": settings.ollama_model, "stream": False, "format": "json",
                      "messages": [{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}]},
            )
            response.raise_for_status()
            content = response.json().get("message", {}).get("content", "{}")
        parsed = json.loads(content)
        suggestions = []
        field_by_key = {f.get("key"): f for f in fields}
        for item in parsed.get("suggestions", []):
            key, source_key = item.get("key"), item.get("source_key")
            field = field_by_key.get(key)
            if not field or field.get("needs_human_review") or source_key not in sources:
                continue
            value = sources[source_key]
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0))))
            # For choice controls, never accept a model-created option.
            if field.get("options"):
                choices = [str(o.get("label", "")).strip().lower() for o in field["options"]]
                if str(value).strip().lower() not in choices:
                    continue
            suggestions.append({"key": key, "source_key": source_key, "value": value,
                                "confidence": confidence,
                                "reason": str(item.get("reason", "Semantic match to saved profile data."))[:240],
                                "label": field.get("label") or field.get("name") or key})
        return {"suggestions": suggestions, "model": settings.ollama_model, "mode": "ollama_semantic"}
    except Exception as exc:
        # A deterministic fallback keeps the feature useful if Ollama is offline.
        from .form_engine import suggest_profile_value
        fallback = []
        for field in fields:
            if field.get("needs_human_review"):
                continue
            value, reason = suggest_profile_value(field, profile, preferences)
            if value not in (None, ""):
                fallback.append({"key": field["key"], "source_key": "deterministic_profile_match",
                                 "value": value, "confidence": 0.65, "reason": reason,
                                 "label": field.get("label") or field.get("name") or field["key"]})
        return {"suggestions": fallback, "model": settings.ollama_model,
                "mode": "deterministic_fallback", "warning": f"Ollama semantic mapping unavailable: {str(exc)[:180]}"}
