import json
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

    # Map extensions to MIME types for multipart upload to PS
    _MIME_MAP = {
        ".pdf":   "application/pdf",
        ".doc":   "application/msword",
        ".docx":  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docm":  "application/vnd.ms-word.document.macroEnabled.12",
        ".dot":   "application/msword",
        ".dotm":  "application/vnd.ms-word.template.macroEnabled.12",
        ".xls":   "application/vnd.ms-excel",
        ".xlsx":  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".ppt":   "application/vnd.ms-powerpoint",
        ".pptx":  "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptm":  "application/vnd.ms-powerpoint.presentation.macroEnabled.12",
        ".pot":   "application/vnd.ms-powerpoint",
        ".odt":   "application/vnd.oasis.opendocument.text",
        ".ods":   "application/vnd.oasis.opendocument.spreadsheet",
        ".odp":   "application/vnd.oasis.opendocument.presentation",
        ".rtf":   "application/rtf",
        ".csv":   "text/csv",
        ".tsv":   "text/tab-separated-values",
        ".txt":   "text/plain",
        ".html":  "text/html",
        ".htm":   "text/html",
        ".xml":   "application/xml",
        ".md":    "text/markdown",
        ".epub":  "application/epub+zip",
        ".eml":   "message/rfc822",
        ".msg":   "application/vnd.ms-outlook",
        ".png":   "image/png",
        ".jpg":   "image/jpeg",
        ".jpeg":  "image/jpeg",
        ".bmp":   "image/bmp",
        ".tiff":  "image/tiff",
        ".tif":   "image/tiff",
        ".gif":   "image/gif",
        ".webp":  "image/webp",
        ".heic":  "image/heic",
        ".zip":   "application/zip",
    }

    async def sanitize_file(self, file_bytes: bytes, filename: str) -> dict:
        """Submit file to PS and poll for result. Returns (result, request_info) tuple.

        Step 1 — POST /api/sanitizeFile  (multipart file upload) → { jobId }
        Step 2 — GET  /api/sanitizeFile?jobId=<id>              → result when ready
        """
        import asyncio
        url = f"{self.base_url}/api/sanitizeFile"
        headers = {"APP-ID": self.app_id}
        request_info = {
            "step1": {
                "method": "POST",
                "url": url,
                "headers": {"APP-ID": f"{self.app_id[:6]}…"},
                "form": {"file": filename},
            },
        }

        # Step 1: submit
        ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
        mime = self._MIME_MAP.get(ext, "application/octet-stream")
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(url, headers=headers, files={"file": (filename, file_bytes, mime)})
                resp.raise_for_status()
                submit_data = resp.json()
                logger.info("PS sanitizeFile submit: %s", json.dumps(submit_data, default=str)[:300])
        except httpx.HTTPStatusError as e:
            logger.error("PS sanitizeFile submit error %s: %s", e.response.status_code, e.response.text)
            raise
        except httpx.RequestError as e:
            logger.error("PS sanitizeFile submit connection error: %s", e)
            raise

        job_id = submit_data.get("jobId")
        if not job_id:
            # API returned a direct result with no jobId — treat as synchronous response
            request_info["step2"] = None
            return submit_data, request_info

        request_info["step2"] = {
            "method": "GET",
            "url": f"{url}?jobId={job_id}",
            "headers": {"APP-ID": f"{self.app_id[:6]}…"},
        }

        # Step 2: poll until complete
        deadline = __import__("time").time() + 60
        while True:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(url, params={"jobId": job_id}, headers=headers)
                    resp.raise_for_status()
                    result = resp.json()
                    logger.info("PS sanitizeFile poll jobId=%s: %s", job_id, json.dumps(result, default=str)[:300])
                    if result.get("status") != "processing":
                        result.setdefault("jobId", job_id)
                        return result, request_info
            except httpx.HTTPStatusError as e:
                logger.error("PS sanitizeFile poll error %s: %s", e.response.status_code, e.response.text)
                raise
            except httpx.RequestError as e:
                logger.error("PS sanitizeFile poll connection error: %s", e)
                raise
            if __import__("time").time() >= deadline:
                raise TimeoutError(f"File sanitization job {job_id} did not complete within 60s")
            await asyncio.sleep(5.0)

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
