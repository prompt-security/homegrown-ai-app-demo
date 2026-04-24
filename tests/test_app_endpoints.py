"""Basic API behavior tests for auth, health, and admin authorization."""

import os
import pytest


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
