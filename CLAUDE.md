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

**Single-file FastAPI backend** (`app/main.py`, ~4200 lines) with all routes. No separate router files — everything is in `main.py`. Supporting modules are thin:

- `models.py` — SQLAlchemy ORM (async): `PSTenant`, `User`, `ChatSession`, `Message`, `APIKey`, `AuditEvent`
- `schemas.py` — Pydantic v2 request/response types
- `auth.py` — JWT issuance/validation, API key hashing, `require_admin` dependency
- `crypto.py` — Fernet encryption for LLM API keys and PS App IDs stored in DB
- `database.py` — async SQLAlchemy engine + `get_db` session dependency
- `prompt_security.py` — `PromptSecurityClient`: wraps `POST /api/protect` and `POST /api/sanitizeFile`
- `token_counter.py` — token estimation via LiteLLM
- `app/static/` — three self-contained HTML files (no build step, no npm): `index.html` (chat UI), `admin.html` (dashboard), `login.html`

**LiteLLM** runs as a separate Docker service on port 4000, configured via `litellm/config.yaml`. The FastAPI app talks to it over the OpenAI-compatible API using `AsyncOpenAI(base_url=LITELLM_BASE_URL)`.

**Direct provider routing** — when a shared API key is saved for OpenAI, Anthropic, Google, Perplexity, or OpenRouter in the admin Settings panel, the app queries that provider's `/models` endpoint and adds all available models to the picker as `provider/model-id` IDs (e.g. `openai/gpt-4.1`). These calls bypass LiteLLM entirely via `_user_llm_client()` / `_guest_llm_client()`. Discovered models are persisted in the `AppSetting` table.

### Key data flows

**Chat (streaming):** `POST /chat/stream` → PS prompt scan (API mode) or pass-through (gateway mode) → LLM call (LiteLLM proxy for config-file models, or direct provider API for `provider/`-prefixed models) → PS response scan → SSE to browser. Gateway mode routes through the PS proxy URL instead of calling PS explicitly.

**File scan:** `POST /upload/sanitize` or `POST /guest/upload/sanitize` → PS two-step async API: `POST /api/sanitizeFile` (returns `jobId`) → `GET /api/sanitizeFile?jobId=X` (poll until `status=done`) → findings rendered with per-category chips and entity detail rows. Result fields live under `metadata.findings` in the PS response.

**Stored secrets:** User LLM API keys and PS App IDs are Fernet-encrypted before DB storage (`crypto.py`). The `ENCRYPTION_KEY` env var must be a valid Fernet key.

**Audit log:** Config changes (PS settings, LLM keys, user/tenant CRUD) write `AuditEvent` rows alongside chat `Message` rows; both appear in the admin activity log.

### Environment variables that change runtime behavior
- `SHOW_LLM_KEY_SETTINGS` — shows per-user LLM key fields in the UI
- `APP_ENV` / `ENV` — used for environment detection
- `DEFAULT_DAILY_LIMIT` — per-user message cap (null = unlimited)
- `MAX_FILE_SIZE_MB` — upload size limit (default 10 MB)
- `SANITIZE_MAX_PER_MINUTE` — rate limit for file scans per user (default 5)
- `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` / `PERPLEXITY_API_KEY` / `OPENROUTER_API_KEY` — shared provider keys (can also be set via Admin → Settings)

---

## Open Mode vs User Mode — Development Checklist

The app runs in two modes controlled by the `user_mgmt_enabled` AppSetting:

| | **Open Mode** (`user_mgmt_enabled=false`) | **User Mode** (`user_mgmt_enabled=true`) |
|---|---|---|
| Auth token | None (`AUTH_TOKEN` is null in frontend) | JWT from `/auth/login`, stored as `hgapp_token` |
| PS config | Client-side only — `hgapp_open_ps_config` in localStorage | Server-side — `User.ps_api_key_enc` + `User.ps_tenant` in DB |
| API calls | `authFetch` rewrites `/admin/rag/` → `/guest/rag/`; no auth header | `authFetch` sends `Authorization: Bearer <token>` |
| Secrets | Encrypted client-side via `_psEncrypt` / `_psDecrypt` | Fernet-encrypted server-side via `crypto.py` |

**Rule: always verify new features work in both modes before committing.**

### Common pitfalls

- **`authFetch` 401 → redirect loop**: In open mode `AUTH_TOKEN` is null. Any `authFetch` call to an auth-required endpoint sends `Bearer null`, gets 401, and the handler redirects to `/login` — which itself redirects back to `/` in open mode. If a feature calls an auth endpoint, add a guest equivalent or an open-mode branch.
- **PS config source mismatch**: Backend endpoints that call `_build_ps_api_client(user)` read the **server-side** PS config. In open mode the guest's PS config is in localStorage, not the DB. Guest endpoints must accept `ps_base_url` + `ps_app_id` in the request body (see `/guest/rag/load-poisoned`) and use them when present.
- **State mutations via API in open mode**: Anything that PATCHes user state (`/users/me/ps-config`, etc.) requires a logged-in user. In open mode, mirror the mutation locally: update `AUTH_USER` in memory and persist to the relevant localStorage key (`hgapp_open_ps_config`, `hgapp_user`, etc.).
- **Guest endpoint gating**: All `/guest/` endpoints that mutate state must call `_require_open_mode(db)` at the top to return 403 in user mode — prevents unauthenticated writes on user-mode deployments.

### Guest endpoint pattern

```python
@app.post("/guest/rag/some-action", status_code=201)
async def guest_some_action(body: dict, db: AsyncSession = Depends(get_db)):
    await _require_open_mode(db)           # 403 if not open mode
    admin = await _get_admin_user(db)      # for audit logging
    ps_base_url = (body.get("ps_base_url") or "").strip()
    ps_app_id   = (body.get("ps_app_id")   or "").strip()
    ps_client = (PromptSecurityClient(base_url=ps_base_url, app_id=ps_app_id)
                 if ps_base_url and ps_app_id else _build_ps_api_client(admin))
    ...
```

### Frontend open-mode branch pattern

```javascript
if (OPEN_MODE) {
    // update AUTH_USER and localStorage directly — no API call
    AUTH_USER = { ...AUTH_USER, ps_enabled: enable };
    const cfg = JSON.parse(localStorage.getItem('hgapp_open_ps_config') || '{}');
    localStorage.setItem('hgapp_open_ps_config', JSON.stringify({ ...cfg, enabled: enable }));
    updatePsStatus();
    return;
}
// user mode — call the API
const res = await authFetch('/users/me/ps-config', { method: 'PATCH', body: JSON.stringify({ ps_enabled: enable }) });
```

---

## Demo Scenario Translations

The demo panel supports multi-language PII prompts via a language picker (`<select>`) shown per country when translations exist. Translations use a **dual-write** pattern so existing deployments pick them up without DB reload.

### Architecture

| Layer | Where | How loaded |
|---|---|---|
| DB seed | `app/data/scenarios.json` — `meta.prompt_XX` keys | Fresh seed only (count == 0) |
| Frontend hardcode | `_SCENARIO_TRANSLATIONS` in `app/static/index.html` | Merged at runtime by `_applyBuiltinTranslations()` |
| Registry | `app/data/translations.py` | Imported by tests |

### Language codes (LANG_NAMES)

| Code | Language | Countries |
|---|---|---|
| `en` | English | All |
| `hi` | हिन्दी | IN |
| `he` | עברית | IL |
| `zh` | 中文 | SG |
| `de` | Deutsch | DE |
| `ja` | 日本語 | JP |
| `pt` | Português | BR |
| `ms` | Bahasa Malaysia | MY |

### Adding a translation to an existing country

1. Add `meta.prompt_XX` to the scenario in `app/data/scenarios.json`
2. Add the same entry to `_SCENARIO_TRANSLATIONS[key]` in `app/static/index.html`
3. **Add a PS API test** to `tests/test_pii_translations.py` — parametrize with `(key, lang, expected_entities)`. Also add `(key, lang)` to `_registered_translations()`. **CI fails if a `prompt_XX` key exists without a test.**
4. Entity types to expect: see `tests/fixtures/ps_policy_reference.json` per country code.
5. Source of truth for entity names: `~/Documents/git/prompt_repos/ps-ai-engine/apps/ps-sensitive-data/`

### Adding a new country with translations

Same checklist as above, plus:
- Add `LANG_NAMES[code]` entry to both `app/static/index.html` **and** `app/data/translations.py`
- For injection-type scenarios: assert `action == "block"`; for PII: assert `action == "modify"`
- Countries with English-only (US, AU, GB): no translation needed, no picker shown

### Running PS API tests locally

```bash
export PS_BASE_URL=https://your-tenant.promptsecurity.ai
export PS_APP_ID=your-app-id
pytest tests/test_pii_translations.py -v
```

Without credentials: all PS tests skip (yellow in CI), none fail.

---

## Session Log

- **2026-06-09** — Session log section added; CLAUDE.md pre-existed.
- **2026-06-15** — Multi-country language picker feature: `<select>` dropdowns for 7 PII countries + Prompt Injection; real PS API tests with coverage guard; CLAUDE.md translation guide.
