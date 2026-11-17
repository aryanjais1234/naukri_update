from __future__ import annotations

import json
from typing import Any

import httpx

from ..config import settings


async def generate_json(*, instructions: str, payload: dict[str, Any], schema_name: str,
                        schema: dict[str, Any]) -> dict[str, Any]:
    """Call OpenAI's Responses API with strict Structured Outputs using HTTPX."""
    if not settings.openai_api_key:
        raise RuntimeError("OpenAI API key is not configured. Add OPENAI_API_KEY to backend/.env and restart the backend.")
    request_body = {
        "model": settings.openai_model,
        "instructions": instructions,
        "input": json.dumps(payload, ensure_ascii=False),
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "strict": True,
                "schema": schema,
            }
        },
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(75.0, connect=15.0)) as client:
        response = await client.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {settings.openai_api_key}",
                     "Content-Type": "application/json"},
            json=request_body,
        )
    if response.status_code >= 400:
        try:
            message = response.json().get("error", {}).get("message", response.text)
        except Exception:
            message = response.text
        raise RuntimeError(f"OpenAI API error ({response.status_code}): {str(message)[:500]}")
    data = response.json()
    text_parts = []
    for output_item in data.get("output", []):
        if output_item.get("type") != "message":
            continue
        for content_item in output_item.get("content", []):
            if content_item.get("type") in {"output_text", "text"}:
                text_parts.append(content_item.get("text", ""))
    raw = "\n".join(part for part in text_parts if part).strip()
    if not raw:
        raise RuntimeError("OpenAI returned an empty structured response.")
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("OpenAI response was not valid JSON.") from exc
    if not isinstance(result, dict):
        raise RuntimeError("OpenAI response did not match the expected object format.")
    return result
