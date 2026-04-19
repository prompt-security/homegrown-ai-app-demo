import json
import time
import httpx
import logging
from typing import Literal, Optional

logger = logging.getLogger(__name__)


class PromptSecurityResult:
    def __init__(self, allowed: bool, action: str, modified_text: Optional[str], violations: list, raw: dict, raw_request: Optional[dict] = None):
        self.allowed = allowed
        self.action = action            # "pass" | "modify" | "block"
        self.modified_text = modified_text
        self.violations = violations
        self.raw = raw
        self.raw_request = raw_request or {}

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

    async def sanitize_file_submit(self, file_bytes: bytes, filename: str) -> str:
        url = f"{self.base_url}/api/sanitizeFile"
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    url,
                    headers={"APP-ID": self.app_id},
                    files={"file": (filename, file_bytes)},
                )
                resp.raise_for_status()
                data = resp.json()
                return data["jobId"]
        except httpx.HTTPStatusError as e:
            logger.error("PS sanitizeFile submit error %s: %s", e.response.status_code, e.response.text)
            raise
        except httpx.RequestError as e:
            logger.error("PS sanitizeFile connection error: %s", e)
            raise

    async def sanitize_file_poll(self, job_id: str, max_seconds: int = 30) -> dict:
        url = f"{self.base_url}/api/sanitizeFile"
        deadline = time.time() + max_seconds
        import asyncio
        while True:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(
                        url,
                        params={"jobId": job_id},
                        headers={"APP-ID": self.app_id},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    logger.info("PS sanitizeFile poll jobId=%s: %s", job_id, json.dumps(data, default=str)[:300])
                    if data.get("status") != "processing":
                        return data
            except httpx.HTTPStatusError as e:
                logger.error("PS sanitizeFile poll error %s: %s", e.response.status_code, e.response.text)
                raise
            except httpx.RequestError as e:
                logger.error("PS sanitizeFile poll connection error: %s", e)
                raise
            if time.time() >= deadline:
                raise TimeoutError(f"File sanitization job {job_id} did not complete within {max_seconds}s")
            await asyncio.sleep(1.0)

    async def _call(
        self, payload: dict, scan_type: Literal["prompt", "response"] = "prompt"
    ) -> PromptSecurityResult:
        url = f"{self.base_url}/api/protect"
        raw_request = dict(payload)
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

        logger.info("PS raw response [%s]: %s", scan_type, json.dumps(data, default=str)[:1000])

        result = data.get("result", {})
        section = result.get(scan_type, {})
        action = section.get("action", "pass")

        # Log detected entities from findings
        findings = section.get("findings", {})
        for detector_name, detections in findings.items():
            if not isinstance(detections, list):
                continue
            for d in detections:
                if isinstance(d, dict) and "entity_type" in d:
                    logger.info("PS [%s] detected: %s = %r (score=%.2f, sanitized=%s)",
                                scan_type, d["entity_type"], d.get("entity", ""),
                                d.get("score", 0), d.get("sanitized_entity", ""))
        allowed = action != "block"
        modified_text = section.get("modified_text") if action == "modify" else None

        # Extract violations — PS may return them at section level or result level
        violations = section.get("violations", [])
        if not violations:
            violations = result.get("violations", [])

        # Normalize violations to a consistent format for the frontend
        normalized = []
        for v in violations:
            if isinstance(v, str):
                normalized.append({"type": v})
            elif isinstance(v, dict):
                # Ensure a displayable type/name field exists
                v.setdefault("type", v.get("category", v.get("name", v.get("entity_type", "unknown"))))
                normalized.append(v)
            else:
                normalized.append({"type": str(v)})

        logger.info("PS [%s] → action=%s violations=%s", scan_type, action, normalized)
        return PromptSecurityResult(
            allowed=allowed, action=action,
            modified_text=modified_text, violations=normalized, raw=data,
            raw_request=raw_request,
        )
