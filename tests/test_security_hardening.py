"""Security hardening regression tests."""

from pathlib import Path

import pytest


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
    html = Path("app/static/index.html").read_text(encoding="utf-8")

    assert "dompurify" in html.lower()
    assert "function sanitizeHtml(" in html
    assert "DOMPurify.sanitize" in html
    assert "bub.innerHTML = sanitizeHtml(rawHtml);" in html
