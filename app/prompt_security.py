import httpx
import logging
from typing import Literal, Optional

logger = logging.getLogger(__name__)


class PromptSecurityResult:
    def __init__(self, allowed: bool, action: str, modified_text: Optional[str], violations: list, raw: dict):
        self.allowed = allowed
        self.action = action            # "pass" | "modify" | "block"
        self.modified_text = modified_text
        self.violations = violations
        self.raw = raw

    def __repr__(self):
        return f"PromptSecurityResult(allowed={self.allowed}, action={self.action}, violations={self.violations})"


class PromptSecurityClient:
    """
    Thin wrapper around the Prompt Security HTTP API.

    POST /api/protect
    Headers: APP-ID: <app_id>
    Body:    { "prompt": str, "system_prompt": str, "response": str, "user": str }
    Response: { "result": { "prompt": { "action": "block"|"modify"|"pass", "modified_text": str },
                            "response": { "action": ..., "modified_text": str } } }
    """

    def __init__(self, base_url: str, app_id: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.app_id = app_id
        self.timeout = timeout
        self._headers = {"APP-ID": app_id, "Content-Type": "application/json"}

    async def protect_prompt(
        self,
        user_prompt: str,
        system_prompt: Optional[str] = None,
        user: Optional[str] = None,
    ) -> PromptSecurityResult:
        payload: dict = {"prompt": user_prompt}
        if system_prompt:
            payload["system_prompt"] = system_prompt
        if user:
            payload["user"] = user
        return await self._call(payload, scan_type="prompt")

    async def protect_response(
        self,
        response_text: str,
        user_prompt: Optional[str] = None,
        system_prompt: Optional[str] = None,
        user: Optional[str] = None,
    ) -> PromptSecurityResult:
        payload: dict = {"response": response_text}
        if user_prompt:
            payload["prompt"] = user_prompt
        if system_prompt:
            payload["system_prompt"] = system_prompt
        if user:
            payload["user"] = user
        return await self._call(payload, scan_type="response")

    async def _call(
        self, payload: dict, scan_type: Literal["prompt", "response"] = "prompt"
    ) -> PromptSecurityResult:
        url = f"{self.base_url}/api/protect"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload, headers=self._headers)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:
            logger.error("PS API error %s: %s", e.response.status_code, e.response.text)
            raise
        except httpx.RequestError as e:
            logger.error("PS connection error: %s", e)
            raise

        section = data.get("result", {}).get(scan_type, {})
        action = section.get("action", "pass")
        allowed = action != "block"
        modified_text = section.get("modified_text") if action == "modify" else None
        violations = section.get("violations", [])

        logger.info("PS [%s] → action=%s violations=%s", scan_type, action, violations)
        return PromptSecurityResult(
            allowed=allowed, action=action,
            modified_text=modified_text, violations=violations, raw=data,
        )
