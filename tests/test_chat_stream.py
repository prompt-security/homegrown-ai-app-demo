"""Integration tests for /chat/stream endpoint — Sprint 1 focus on PS behavior."""

import json
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from prompt_security import PromptSecurityClient, PromptSecurityResult


# ── Helpers ──────────────────────────────────────────────────────────────────


def make_ps_result(action="pass", violations=None, modified_text=None):
    """Build a PromptSecurityResult for mocking."""
    return PromptSecurityResult(
        allowed=(action != "block"),
        action=action,
        modified_text=modified_text,
        violations=violations or [],
        raw={},
    )


def parse_sse_events(raw: str) -> list[dict]:
    """Parse SSE text into a list of JSON event dicts."""
    events = []
    for line in raw.strip().split("\n\n"):
        for part in line.split("\n"):
            if part.startswith("data: "):
                try:
                    events.append(json.loads(part[6:]))
                except json.JSONDecodeError:
                    pass
    return events


# ── Unit tests for PS call pattern (prompt vs response) ──────────────────────


async def test_protect_response_not_called_with_prompt():
    """Verify that protect_response is called WITHOUT prompt/system_prompt args."""
    client = PromptSecurityClient(base_url="https://test.ps", app_id="test")

    # Mock _call to capture what protect_response sends
    call_payloads = []
    original_call = client._call

    async def capturing_call(payload, scan_type="prompt"):
        call_payloads.append((scan_type, payload))
        return make_ps_result()

    client._call = capturing_call

    # Simulate what main.py does after Sprint 1 fix:
    # protect_prompt sends prompt + system_prompt
    await client.protect_prompt(
        user_prompt="Hello",
        system_prompt="Be helpful",
        user="user@test.com",
    )

    # protect_response should only send response + user (no prompt or system_prompt)
    await client.protect_response(
        response_text="Here is my answer",
        user="user@test.com",
    )

    assert len(call_payloads) == 2

    # First call: prompt scan
    scan_type_1, payload_1 = call_payloads[0]
    assert scan_type_1 == "prompt"
    assert "prompt" in payload_1
    assert "system_prompt" in payload_1

    # Second call: response scan — should NOT contain prompt
    scan_type_2, payload_2 = call_payloads[1]
    assert scan_type_2 == "response"
    assert "response" in payload_2
    assert "user" in payload_2
    assert "prompt" not in payload_2, "prompt should not be sent in response scan"
    assert "system_prompt" not in payload_2, "system_prompt should not be sent in response scan"


async def test_blocked_prompt_includes_violations():
    """When PS blocks a prompt, violations should be returned in the result."""
    client = PromptSecurityClient(base_url="https://test.ps", app_id="test")

    violations = [
        {"type": "topic_detector", "score": 0.95},
        {"type": "harmful_content", "score": 0.8},
    ]

    async def mock_call(payload, scan_type="prompt"):
        return make_ps_result(action="block", violations=violations)

    client._call = mock_call

    result = await client.protect_prompt(user_prompt="Fire the employee")
    assert result.allowed is False
    assert result.action == "block"
    assert len(result.violations) == 2
    assert result.violations[0]["type"] == "topic_detector"
    assert result.violations[1]["type"] == "harmful_content"


async def test_blocked_response_includes_violations():
    """When PS blocks a response, violations should be returned."""
    client = PromptSecurityClient(base_url="https://test.ps", app_id="test")

    violations = [{"type": "sensitive_data", "category": "PII", "score": 0.99}]

    async def mock_call(payload, scan_type="prompt"):
        return make_ps_result(action="block", violations=violations)

    client._call = mock_call

    result = await client.protect_response(response_text="SSN: 123-45-6789")
    assert result.allowed is False
    assert len(result.violations) == 1
    assert result.violations[0]["type"] == "sensitive_data"


async def test_modified_prompt_returns_sanitized_text():
    """When PS modifies a prompt, modified_text should be returned."""
    client = PromptSecurityClient(base_url="https://test.ps", app_id="test")

    async def mock_call(payload, scan_type="prompt"):
        return make_ps_result(
            action="modify",
            modified_text="My SSN is [REDACTED]",
            violations=[{"type": "US_SSN", "score": 0.99}],
        )

    client._call = mock_call

    result = await client.protect_prompt(user_prompt="My SSN is 123-45-6789")
    assert result.allowed is True
    assert result.action == "modify"
    assert result.modified_text == "My SSN is [REDACTED]"
    assert len(result.violations) == 1


async def test_two_scan_calls_pattern():
    """Simulate the full prompt+response scan flow and verify call count."""
    client = PromptSecurityClient(base_url="https://test.ps", app_id="test")

    call_count = 0

    async def counting_call(payload, scan_type="prompt"):
        nonlocal call_count
        call_count += 1
        return make_ps_result()

    client._call = counting_call

    # Prompt scan
    await client.protect_prompt(user_prompt="Hello", system_prompt="Be helpful", user="u@t.com")
    # Response scan (no prompt/system_prompt)
    await client.protect_response(response_text="Answer", user="u@t.com")

    assert call_count == 2, f"Expected exactly 2 PS API calls, got {call_count}"
