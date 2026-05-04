"""Security hardening regression tests."""

import os
from types import SimpleNamespace
from hashlib import sha256
from pathlib import Path

import pytest
from fastapi import HTTPException

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _chdir_to_app():
    original = os.getcwd()
    os.chdir(REPO_ROOT / "app")
    yield
    os.chdir(original)


def test_validate_security_bootstrap_config_rejects_insecure_defaults(monkeypatch):
    import main

    monkeypatch.setattr(main, "APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "dev_secret_change_me")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin")

    with pytest.raises(RuntimeError):
        main._validate_security_bootstrap_config()


def test_validate_security_bootstrap_config_allows_secure_values(monkeypatch):
    import main

    monkeypatch.setattr(main, "APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "super-long-random-secret-value-12345")
    monkeypatch.setenv("ADMIN_PASSWORD", "StrongAdminPass!123")

    main._validate_security_bootstrap_config()


def test_frontend_uses_dompurify_for_markdown_rendering():
    html = (REPO_ROOT / "app/static/index.html").read_text(encoding="utf-8")

    assert "dompurify" in html.lower()
    assert "function sanitizeHtml(" in html
    assert "DOMPurify.sanitize" in html
    assert "bub.innerHTML = sanitizeHtml(rawHtml);" in html


def test_admin_chart_script_is_pinned_with_sri():
    html = (REPO_ROOT / "app/static/admin.html").read_text(encoding="utf-8")

    assert "chart.js@4.4.9" in html
    assert 'integrity="sha384-' in html
    assert 'crossorigin="anonymous"' in html


def test_frontend_only_disables_models_that_require_missing_keys():
    html = (REPO_ROOT / "app/static/index.html").read_text(encoding="utf-8")

    assert "if (m.requires_key && !m.key_set)" in html


def test_frontend_gates_compare_mode_to_admins():
    html = (REPO_ROOT / "app/static/index.html").read_text(encoding="utf-8")

    assert "function canUseCompareMode()" in html
    assert "AUTH_USER?.role === 'admin'" in html
    assert "const effectiveSkipPs = Boolean(skipPs && canUseCompareMode());" in html


def test_dockerfile_uses_non_root_runtime_user():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "\nUSER appuser\n" in dockerfile


def test_api_key_hash_is_keyed():
    from auth import hash_api_key

    raw_key = "hg_live_example"
    assert hash_api_key(raw_key) == hash_api_key(raw_key)
    assert hash_api_key(raw_key) != sha256(raw_key.encode()).hexdigest()


@pytest.mark.asyncio
async def test_legacy_api_key_hash_is_accepted_and_migrated(db, test_user):
    from auth import get_current_api_key, hash_api_key
    from models import APIKey

    raw_key = "hg_live_legacy_example"
    key = APIKey(
        user_id=test_user.id,
        name="legacy",
        key_hash=sha256(raw_key.encode()).hexdigest(),
        key_prefix="hg_live_",
        is_active=True,
    )
    db.add(key)
    await db.commit()
    await db.refresh(key)

    request = SimpleNamespace(headers={"authorization": f"Bearer {raw_key}"})
    api_key, user = await get_current_api_key(request, db)

    assert api_key.id == key.id
    assert user.id == test_user.id
    assert api_key.key_hash == hash_api_key(raw_key)
    assert api_key.key_hash != sha256(raw_key.encode()).hexdigest()


@pytest.mark.parametrize(
    "url",
    [
        "http://prompt.security",
        "https://localhost",
        "https://127.0.0.1",
        "https://10.0.0.1",
        "https://169.254.169.254",
    ],
)
def test_external_url_validation_rejects_unsafe_targets(url):
    import main

    with pytest.raises(HTTPException):
        main._validate_external_https_url(url, "gateway_url")


def test_external_url_validation_allows_https_hostnames():
    import main

    assert (
        main._validate_external_https_url("https://test.prompt.security/v1", "gateway_url")
        == "https://test.prompt.security/v1"
    )


def test_legacy_public_http_url_can_be_normalized():
    import main

    assert main._normalize_legacy_public_http_url("http://test.prompt.security/api") == "https://test.prompt.security/api"
    assert main._normalize_legacy_public_http_url("http://localhost/api") is None
    assert main._normalize_legacy_public_http_url("http://10.0.0.1/api") is None


@pytest.mark.asyncio
async def test_invalid_persisted_ps_base_url_soft_fails(db, test_user, test_tenant):
    import main
    from crypto import encrypt

    test_tenant.base_url = "http://localhost"
    test_user.ps_tenant_id = test_tenant.id
    test_user.ps_tenant = test_tenant
    test_user.ps_api_key_enc = encrypt("app-id")
    test_user.ps_enabled = True

    assert main._build_ps_api_client(test_user) is None


@pytest.mark.asyncio
async def test_legacy_invalid_ps_tenant_disables_existing_users(db, test_user, test_tenant):
    import main

    test_tenant.base_url = "http://localhost"
    test_user.ps_tenant_id = test_tenant.id
    test_user.ps_enabled = True
    await db.commit()

    await main._migrate_legacy_ps_tenant_urls(db)
    await db.refresh(test_user)

    assert test_user.ps_enabled is False
