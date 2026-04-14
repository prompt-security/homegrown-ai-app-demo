"""Tests for the PromptSecurityClient."""

import pytest
import respx
import httpx

from prompt_security import PromptSecurityClient


BASE_URL = "https://test.prompt.security"
APP_ID = "test-app-id"


@pytest.fixture
def ps_client():
    return PromptSecurityClient(base_url=BASE_URL, app_id=APP_ID)


# ── protect_prompt tests ─────────────────────────────────────────────────────


@respx.mock
async def test_protect_prompt_sends_only_prompt_fields(ps_client):
    """protect_prompt should send prompt, system_prompt, user — NOT response."""
    captured_payload = {}

    route = respx.post(f"{BASE_URL}/api/protect").mock(
        return_value=httpx.Response(200, json={
            "status": "success",
            "result": {"prompt": {"action": "pass", "violations": []}}
        })
    )

    await ps_client.protect_prompt(
        user_prompt="Hello world",
        system_prompt="Be helpful",
        user="test@example.com",
    )

    assert route.called
    payload = route.calls[0].request.content
    import json
    body = json.loads(payload)
    assert "prompt" in body
    assert body["prompt"] == "Hello world"
    assert body["system_prompt"] == "Be helpful"
    assert body["user"] == "test@example.com"
    assert "response" not in body


@respx.mock
async def test_protect_prompt_pass(ps_client):
    """PS returns pass — result should be allowed with no modifications."""
    respx.post(f"{BASE_URL}/api/protect").mock(
        return_value=httpx.Response(200, json={
            "status": "success",
            "result": {"prompt": {"action": "pass", "violations": []}}
        })
    )

    result = await ps_client.protect_prompt(user_prompt="Hello")
    assert result.allowed is True
    assert result.action == "pass"
    assert result.modified_text is None
    assert result.violations == []


@respx.mock
async def test_protect_prompt_block(ps_client):
    """PS blocks a prompt — result should have allowed=False."""
    respx.post(f"{BASE_URL}/api/protect").mock(
        return_value=httpx.Response(200, json={
            "status": "success",
            "result": {"prompt": {
                "action": "block",
                "violations": [{"type": "topic_detector", "score": 0.95}]
            }}
        })
    )

    result = await ps_client.protect_prompt(user_prompt="Fire the employee")
    assert result.allowed is False
    assert result.action == "block"
    assert len(result.violations) == 1
    assert result.violations[0]["type"] == "topic_detector"


@respx.mock
async def test_protect_prompt_modify(ps_client):
    """PS modifies (sanitizes) a prompt — result should include modified_text."""
    respx.post(f"{BASE_URL}/api/protect").mock(
        return_value=httpx.Response(200, json={
            "status": "success",
            "result": {"prompt": {
                "action": "modify",
                "modified_text": "Hello, my SSN is [REDACTED]",
                "violations": [{"type": "US_SSN", "score": 0.99}]
            }}
        })
    )

    result = await ps_client.protect_prompt(user_prompt="Hello, my SSN is 123-45-6789")
    assert result.allowed is True
    assert result.action == "modify"
    assert result.modified_text == "Hello, my SSN is [REDACTED]"
    assert result.violations[0]["type"] == "US_SSN"


# ── protect_response tests ───────────────────────────────────────────────────


@respx.mock
async def test_protect_response_sends_only_response_fields(ps_client):
    """protect_response should send response + user, NOT prompt or system_prompt."""
    route = respx.post(f"{BASE_URL}/api/protect").mock(
        return_value=httpx.Response(200, json={
            "status": "success",
            "result": {"response": {"action": "pass", "violations": []}}
        })
    )

    await ps_client.protect_response(
        response_text="Here is the answer",
        user="test@example.com",
    )

    assert route.called
    import json
    body = json.loads(route.calls[0].request.content)
    assert "response" in body
    assert body["response"] == "Here is the answer"
    assert body["user"] == "test@example.com"
    # prompt and system_prompt should NOT be sent (fix for "prompt sent twice")
    assert "prompt" not in body
    assert "system_prompt" not in body


@respx.mock
async def test_protect_response_block(ps_client):
    """PS blocks a response — result should have allowed=False."""
    respx.post(f"{BASE_URL}/api/protect").mock(
        return_value=httpx.Response(200, json={
            "status": "success",
            "result": {"response": {
                "action": "block",
                "violations": [{"type": "sensitive_data", "category": "PII"}]
            }}
        })
    )

    result = await ps_client.protect_response(response_text="SSN: 123-45-6789")
    assert result.allowed is False
    assert result.action == "block"
    assert len(result.violations) == 1


# ── Violation parsing tests ──────────────────────────────────────────────────


@respx.mock
async def test_violations_normalized_from_strings(ps_client):
    """String violations should be normalized to {type: ...} dicts."""
    respx.post(f"{BASE_URL}/api/protect").mock(
        return_value=httpx.Response(200, json={
            "status": "success",
            "result": {"prompt": {
                "action": "block",
                "violations": ["topic_detector", "harmful_content"]
            }}
        })
    )

    result = await ps_client.protect_prompt(user_prompt="test")
    assert result.violations == [{"type": "topic_detector"}, {"type": "harmful_content"}]


@respx.mock
async def test_violations_normalized_from_objects(ps_client):
    """Object violations should get a type field from category/name fallback."""
    respx.post(f"{BASE_URL}/api/protect").mock(
        return_value=httpx.Response(200, json={
            "status": "success",
            "result": {"prompt": {
                "action": "modify",
                "modified_text": "redacted",
                "violations": [
                    {"category": "PII", "score": 0.95},
                    {"name": "US_SSN", "confidence": 0.99, "start": 10, "end": 21},
                    {"entity_type": "EMAIL_ADDRESS"},
                ]
            }}
        })
    )

    result = await ps_client.protect_prompt(user_prompt="test")
    assert result.violations[0]["type"] == "PII"
    assert result.violations[1]["type"] == "US_SSN"
    assert result.violations[1]["start"] == 10
    assert result.violations[2]["type"] == "EMAIL_ADDRESS"


@respx.mock
async def test_violations_fallback_to_result_level(ps_client):
    """If section-level violations are empty, try result-level."""
    respx.post(f"{BASE_URL}/api/protect").mock(
        return_value=httpx.Response(200, json={
            "status": "success",
            "result": {
                "prompt": {"action": "block", "violations": []},
                "violations": [{"type": "token_limit", "score": 1.0}]
            }
        })
    )

    result = await ps_client.protect_prompt(user_prompt="test")
    assert len(result.violations) == 1
    assert result.violations[0]["type"] == "token_limit"


@respx.mock
async def test_ps_api_error_raises(ps_client):
    """HTTP errors from PS API should propagate."""
    respx.post(f"{BASE_URL}/api/protect").mock(
        return_value=httpx.Response(401, json={"status": "failed", "reason": "Invalid token"})
    )

    with pytest.raises(httpx.HTTPStatusError):
        await ps_client.protect_prompt(user_prompt="test")


@respx.mock
async def test_ps_headers_correct(ps_client):
    """Request should include APP-ID header."""
    route = respx.post(f"{BASE_URL}/api/protect").mock(
        return_value=httpx.Response(200, json={
            "status": "success",
            "result": {"prompt": {"action": "pass", "violations": []}}
        })
    )

    await ps_client.protect_prompt(user_prompt="test")
    headers = route.calls[0].request.headers
    assert headers["APP-ID"] == APP_ID
    assert headers["Content-Type"] == "application/json"
