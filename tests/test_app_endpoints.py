"""Basic API behavior tests for auth, health, and admin authorization."""

import json
import os
import pytest
from unittest.mock import AsyncMock, patch

from auth import create_access_token, hash_password
from crypto import encrypt
from models import ChatSession, User
from prompt_security import PromptSecurityResult


@pytest.fixture(autouse=True)
def _chdir_to_app():
    """main.py mounts StaticFiles(directory='static') — ensure CWD is app/."""
    original = os.getcwd()
    os.chdir(os.path.join(os.path.dirname(__file__), "..", "app"))
    yield
    os.chdir(original)


@pytest.mark.asyncio
async def test_health_endpoint_is_public(client):
    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "litellm_url" in body
    assert "models_loaded" in body


@pytest.mark.asyncio
async def test_login_returns_access_token(client, test_user):
    response = await client.post(
        "/auth/login",
        json={"email": test_user.email, "password": "password"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["token_type"] == "bearer"
    assert isinstance(payload["access_token"], str)
    assert payload["access_token"]


@pytest.mark.asyncio
async def test_non_admin_cannot_access_admin_stats(client, auth_token):
    response = await client.get(
        "/admin/stats",
        headers={"Authorization": f"Bearer {auth_token}"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Admin access required"


@pytest.mark.asyncio
async def test_upload_reads_with_size_cap(client, auth_token, monkeypatch):
    import main
    from starlette.datastructures import UploadFile as StarletteUploadFile

    calls: list[int] = []
    original_read = StarletteUploadFile.read

    async def _spy_read(self, size=-1):
        calls.append(size)
        return await original_read(self, size)

    monkeypatch.setattr(StarletteUploadFile, "read", _spy_read)

    response = await client.post(
        "/upload",
        files={"file": ("note.txt", b"hello", "text/plain")},
        headers={"Authorization": f"Bearer {auth_token}"},
    )

    assert response.status_code == 200
    assert main.MAX_FILE_SIZE_BYTES + 1 in calls


@pytest.mark.asyncio
async def test_upload_sanitize_rejects_unsupported_type_before_forward(
    client,
    auth_token,
    db,
    test_user,
    test_tenant,
    monkeypatch,
):
    import main

    test_user.ps_tenant_id = test_tenant.id
    test_user.ps_api_key_enc = encrypt("app-id")
    await db.commit()

    submit_mock = AsyncMock()
    poll_mock = AsyncMock()

    class _FakePSClient:
        def __init__(self, *args, **kwargs):
            self.sanitize_file_submit = submit_mock
            self.sanitize_file_poll = poll_mock

    monkeypatch.setattr(main, "PromptSecurityClient", _FakePSClient)

    response = await client.post(
        "/upload/sanitize",
        files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
        headers={"Authorization": f"Bearer {auth_token}"},
    )

    assert response.status_code == 415
    submit_mock.assert_not_awaited()
    poll_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_sanitize_rejects_oversized_file_before_forward(
    client,
    auth_token,
    db,
    test_user,
    test_tenant,
    monkeypatch,
):
    import main

    test_user.ps_tenant_id = test_tenant.id
    test_user.ps_api_key_enc = encrypt("app-id")
    await db.commit()

    submit_mock = AsyncMock()
    poll_mock = AsyncMock()

    class _FakePSClient:
        def __init__(self, *args, **kwargs):
            self.sanitize_file_submit = submit_mock
            self.sanitize_file_poll = poll_mock

    monkeypatch.setattr(main, "PromptSecurityClient", _FakePSClient)

    oversized = b"a" * (main.MAX_FILE_SIZE_BYTES + 1)
    response = await client.post(
        "/upload/sanitize",
        files={"file": ("big.txt", oversized, "text/plain")},
        headers={"Authorization": f"Bearer {auth_token}"},
    )

    assert response.status_code == 413
    submit_mock.assert_not_awaited()
    poll_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_sanitize_rejects_when_concurrency_limit_reached(
    client,
    auth_token,
    db,
    test_user,
    test_tenant,
    monkeypatch,
):
    import main

    test_user.ps_tenant_id = test_tenant.id
    test_user.ps_api_key_enc = encrypt("app-id")
    await db.commit()

    class _FakePSClient:
        def __init__(self, *args, **kwargs):
            self.sanitize_file_submit = AsyncMock()
            self.sanitize_file_poll = AsyncMock()

    monkeypatch.setattr(main, "PromptSecurityClient", _FakePSClient)
    monkeypatch.setitem(main._sanitize_user_active, test_user.id, main.SANITIZE_MAX_CONCURRENT_PER_USER)

    response = await client.post(
        "/upload/sanitize",
        files={"file": ("ok.txt", b"hello", "text/plain")},
        headers={"Authorization": f"Bearer {auth_token}"},
    )

    assert response.status_code == 429
    assert "concurrent" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_upload_sanitize_rejects_when_rate_limit_reached(
    client,
    auth_token,
    db,
    test_user,
    test_tenant,
    monkeypatch,
):
    import main

    test_user.ps_tenant_id = test_tenant.id
    test_user.ps_api_key_enc = encrypt("app-id")
    await db.commit()

    class _FakePSClient:
        def __init__(self, *args, **kwargs):
            self.sanitize_file_submit = AsyncMock()
            self.sanitize_file_poll = AsyncMock()

    monkeypatch.setattr(main, "PromptSecurityClient", _FakePSClient)
    monkeypatch.setattr(main, "SANITIZE_MAX_PER_MINUTE", 1)
    main._sanitize_user_timestamps[test_user.id].clear()
    main._sanitize_user_timestamps[test_user.id].append(main.time.time())

    response = await client.post(
        "/upload/sanitize",
        files={"file": ("ok.txt", b"hello", "text/plain")},
        headers={"Authorization": f"Bearer {auth_token}"},
    )

    assert response.status_code == 429
    assert "rate limit" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_upload_sanitize_unsupported_does_not_consume_rate_limit(
    client,
    auth_token,
    db,
    test_user,
    test_tenant,
    monkeypatch,
):
    import main

    test_user.ps_tenant_id = test_tenant.id
    test_user.ps_api_key_enc = encrypt("app-id")
    await db.commit()

    submit_mock = AsyncMock(return_value="job-1")
    poll_mock = AsyncMock(return_value={"action": "pass", "violations": []})

    class _FakePSClient:
        def __init__(self, *args, **kwargs):
            self.sanitize_file_submit = submit_mock
            self.sanitize_file_poll = poll_mock

    monkeypatch.setattr(main, "PromptSecurityClient", _FakePSClient)
    monkeypatch.setattr(main, "SANITIZE_MAX_PER_MINUTE", 1)
    main._sanitize_user_timestamps[test_user.id].clear()
    main._sanitize_user_active[test_user.id] = 0

    bad_response = await client.post(
        "/upload/sanitize",
        files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert bad_response.status_code == 415

    ok_response = await client.post(
        "/upload/sanitize",
        files={"file": ("ok.txt", b"hello", "text/plain")},
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert ok_response.status_code == 200
    submit_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_upload_sanitize_oversized_does_not_consume_rate_limit(
    client,
    auth_token,
    db,
    test_user,
    test_tenant,
    monkeypatch,
):
    import main

    test_user.ps_tenant_id = test_tenant.id
    test_user.ps_api_key_enc = encrypt("app-id")
    await db.commit()

    submit_mock = AsyncMock(return_value="job-1")
    poll_mock = AsyncMock(return_value={"action": "pass", "violations": []})

    class _FakePSClient:
        def __init__(self, *args, **kwargs):
            self.sanitize_file_submit = submit_mock
            self.sanitize_file_poll = poll_mock

    monkeypatch.setattr(main, "PromptSecurityClient", _FakePSClient)
    monkeypatch.setattr(main, "SANITIZE_MAX_PER_MINUTE", 1)
    main._sanitize_user_timestamps[test_user.id].clear()
    main._sanitize_user_active[test_user.id] = 0

    oversized = b"a" * (main.MAX_FILE_SIZE_BYTES + 1)
    bad_response = await client.post(
        "/upload/sanitize",
        files={"file": ("big.txt", oversized, "text/plain")},
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert bad_response.status_code == 413

    ok_response = await client.post(
        "/upload/sanitize",
        files={"file": ("ok.txt", b"hello", "text/plain")},
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert ok_response.status_code == 200
    submit_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_upload_sanitize_successful_request_consumes_rate_limit(
    client,
    auth_token,
    db,
    test_user,
    test_tenant,
    monkeypatch,
):
    import main

    test_user.ps_tenant_id = test_tenant.id
    test_user.ps_api_key_enc = encrypt("app-id")
    await db.commit()

    submit_mock = AsyncMock(return_value="job-1")
    poll_mock = AsyncMock(return_value={"action": "pass", "violations": []})

    class _FakePSClient:
        def __init__(self, *args, **kwargs):
            self.sanitize_file_submit = submit_mock
            self.sanitize_file_poll = poll_mock

    monkeypatch.setattr(main, "PromptSecurityClient", _FakePSClient)
    monkeypatch.setattr(main, "SANITIZE_MAX_PER_MINUTE", 1)
    main._sanitize_user_timestamps[test_user.id].clear()
    main._sanitize_user_active[test_user.id] = 0

    first_response = await client.post(
        "/upload/sanitize",
        files={"file": ("ok.txt", b"hello", "text/plain")},
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert first_response.status_code == 200

    second_response = await client.post(
        "/upload/sanitize",
        files={"file": ("ok-2.txt", b"hello2", "text/plain")},
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert second_response.status_code == 429
    assert "rate limit" in second_response.json()["detail"].lower()
    assert submit_mock.await_count == 1


@pytest.mark.asyncio
async def test_chat_stream_rejects_overlong_session_id(client, auth_token):
    response = await client.post(
        "/chat/stream",
        headers={"Authorization": f"Bearer {auth_token}"},
        json={
            "messages": [{"role": "user", "content": "hello"}],
            "session_id": "x" * 64,
        },
    )

    assert response.status_code == 422
    assert any(err["loc"][-1] == "session_id" for err in response.json().get("detail", []))


@pytest.mark.asyncio
async def test_chat_stream_rejects_non_uuid_session_id(client, auth_token):
    response = await client.post(
        "/chat/stream",
        headers={"Authorization": f"Bearer {auth_token}"},
        json={
            "messages": [{"role": "user", "content": "hello"}],
            "session_id": "not-a-uuid",
        },
    )

    assert response.status_code == 422
    assert any(err["loc"][-1] == "session_id" for err in response.json().get("detail", []))


@pytest.mark.asyncio
async def test_chat_stream_rejects_other_users_session_id(client, db, test_user):
    other_user = User(
        email="other-owner@test.com",
        hashed_password=hash_password("password"),
        role="se",
        is_active=True,
    )
    db.add(other_user)
    await db.commit()
    await db.refresh(other_user)

    foreign_session_id = "550e8400-e29b-41d4-a716-446655440000"
    db.add(ChatSession(id=foreign_session_id, user_id=other_user.id, title="foreign"))
    await db.commit()

    response = await client.post(
        "/chat/stream",
        headers={"Authorization": f"Bearer {create_access_token({'sub': str(test_user.id)})}"},
        json={
            "messages": [{"role": "user", "content": "hello"}],
            "session_id": foreign_session_id,
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Session not found"


def _parse_sse_events(raw: str) -> list[dict]:
    events = []
    for line in raw.strip().split("\n\n"):
        for part in line.split("\n"):
            if part.startswith("data: "):
                events.append(json.loads(part[6:]))
    return events


def _fake_stream():
    class _Chunk:
        usage = None

        class _Choice:
            class _Delta:
                content = "hello"

            delta = _Delta()

        choices = [_Choice()]

    async def _gen():
        yield _Chunk()

    return _gen()


def _ps_pass_result() -> PromptSecurityResult:
    return PromptSecurityResult(
        allowed=True,
        action="pass",
        modified_text=None,
        violations=[],
        raw={},
        raw_request={},
    )


@pytest.mark.asyncio
async def test_chat_stream_non_admin_cannot_bypass_ps_with_skip_flag(client, db, test_user, test_tenant):
    test_user.ps_tenant_id = test_tenant.id
    test_user.ps_api_key_enc = encrypt("test-app-id")
    test_user.ps_enabled = True
    await db.commit()

    response = await client.post(
        "/chat/stream",
        headers={"Authorization": f"Bearer {create_access_token({'sub': str(test_user.id)})}"},
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "model": "gpt-4o-mini",
            "skip_ps": True,
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "skip_ps is restricted to admin users"


@pytest.mark.asyncio
async def test_chat_stream_admin_can_bypass_ps_with_skip_flag(client, db, test_tenant):
    admin = User(
        email="admin-skip@test.com",
        hashed_password=hash_password("password"),
        role="admin",
        is_active=True,
        ps_tenant_id=test_tenant.id,
        ps_api_key_enc=encrypt("test-app-id"),
        ps_enabled=True,
    )
    db.add(admin)
    await db.commit()
    await db.refresh(admin)

    llm = AsyncMock()
    llm.chat.completions.create = AsyncMock(return_value=_fake_stream())

    with (
        patch("main._user_llm_client", return_value=(llm, "gpt-4o-mini")),
        patch("main.PromptSecurityClient.protect_prompt", new=AsyncMock(return_value=_ps_pass_result())) as mock_prompt,
        patch("main.PromptSecurityClient.protect_response", new=AsyncMock(return_value=_ps_pass_result())) as mock_response,
    ):
        response = await client.post(
            "/chat/stream",
            headers={"Authorization": f"Bearer {create_access_token({'sub': str(admin.id)})}"},
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "model": "gpt-4o-mini",
                "skip_ps": True,
            },
        )

    assert response.status_code == 200
    events = _parse_sse_events(response.text)
    done = next(e for e in events if e.get("type") == "done")
    assert done["ps_scanned"] is False
    assert mock_prompt.await_count == 0
    assert mock_response.await_count == 0
