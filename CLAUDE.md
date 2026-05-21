# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Changelog

This project maintains a `CHANGELOG.md` at the repo root in [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) format (date-based, no semver).

**Rule:** Whenever you implement a feature, fix, or any meaningful change, add an entry to `CHANGELOG.md` under today's date before committing. Use subsections `### Added`, `### Changed`, `### Fixed`, `### Security` as appropriate. Put the newest date at the top.

**Rule:** Each changelog entry must include the author who owns the change. Use only the username portion of their email (the part before `@`) as an incognito identifier, e.g. `— @johndoe`. If Claude implements the change autonomously, attribute it to the user who requested it.

---

## Commands

### Run the app (Docker, recommended)
```bash
docker compose up -d          # start all services
docker compose up -d --build app  # rebuild after Python changes
docker logs -f demo-hgapp-litellm-1  # watch LiteLLM migrations on first run
```

### Run locally without Docker
```bash
docker compose up -d db       # just the Postgres container
pip install -r requirements.txt
cd app && uvicorn main:app --reload --port 8000
```

### Run tests
```bash
pip install -r requirements-test.txt
pytest                         # all tests (uses SQLite in-memory)
pytest tests/test_app_endpoints.py          # single file
pytest tests/test_chat_stream.py::test_name # single test
```

Tests use SQLite in-memory via `conftest.py` — no running Postgres or LiteLLM needed.

---

## Architecture

**Single-file FastAPI backend** (`app/main.py`, ~1800 lines) with all routes. No separate router files — everything is in `main.py`. Supporting modules are thin:

- `models.py` — SQLAlchemy ORM (async): `PSTenant`, `User`, `ChatSession`, `Message`, `APIKey`, `AuditEvent`
- `schemas.py` — Pydantic v2 request/response types
- `auth.py` — JWT issuance/validation, API key hashing, `require_admin` dependency
- `crypto.py` — Fernet encryption for LLM API keys and PS App IDs stored in DB
- `database.py` — async SQLAlchemy engine + `get_db` session dependency
- `prompt_security.py` — `PromptSecurityClient`: wraps `POST /api/protect` and `POST /api/sanitizeFile`
- `token_counter.py` — token estimation via LiteLLM
- `app/static/` — three self-contained HTML files (no build step, no npm): `index.html` (chat UI), `admin.html` (dashboard), `login.html`

**LiteLLM** runs as a separate Docker service on port 4000, configured via `litellm/config.yaml`. The FastAPI app talks to it over the OpenAI-compatible API using `AsyncOpenAI(base_url=LITELLM_BASE_URL)`.

### Key data flows

**Chat (streaming):** `POST /chat/stream` → PS prompt scan (API mode) or pass-through (gateway mode) → LiteLLM SSE → PS response scan → SSE to browser. Gateway mode routes the LiteLLM call through the PS proxy URL instead of calling PS explicitly.

**Stored secrets:** User LLM API keys and PS App IDs are Fernet-encrypted before DB storage (`crypto.py`). The `ENCRYPTION_KEY` env var must be a valid Fernet key.

**Audit log:** Config changes (PS settings, LLM keys, user/tenant CRUD) write `AuditEvent` rows alongside chat `Message` rows; both appear in the admin activity log.

### Environment variables that change runtime behavior
- `PUBLIC_API_ENABLED` — enables `POST /v1/responses` (disabled by default)
- `SHOW_LLM_KEY_SETTINGS` — shows per-user LLM key fields in the UI
- `APP_ENV` / `ENV` — used for environment detection
- `DEFAULT_DAILY_LIMIT` — per-user message cap (null = unlimited)
- `MAX_FILE_SIZE_MB` — upload size limit (default 10 MB)
