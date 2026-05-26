import asyncio
import base64
import email.mime.multipart
import email.mime.text
import io
import json
import logging
import os
import random
import secrets
import smtplib
import time
import uuid
from pathlib import Path
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse, urlunparse

import httpx
import openai
from dotenv import load_dotenv
from pydantic import BaseModel
from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI
from sqlalchemy import desc, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

load_dotenv()

from auth import (
    ALGORITHM, SECRET_KEY, TOKEN_TTL_H,
    create_access_token, create_api_key, get_current_api_key, get_current_user,
    hash_api_key, hash_password, require_admin, reset_secret_key, set_secret_key, verify_password,
)
from crypto import (
    decrypt, encrypt,
    clear_encryption_key_override, encryption_key_overridden,
    set_encryption_key, validate_fernet_key, write_encryption_key_override,
)
import database as _db_module
from database import AsyncSessionLocal, Base, engine, get_db
from models import APIKey, AppSetting, AuditEvent, ChatSession, DemoScenario, Message, PSTenant, User
from prompt_security import PromptSecurityClient
from schemas import (
    APIKeyCreateRequest, APIKeyCreateResponse, APIKeyOut,
    ChatMessage, ChatRequest, ChatResponse,
    LLMKeysUpdate, LoginRequest, MessageOut, PSConfigUpdate, PSTenantCreate, PSTenantOut, PSTenantUpdate,
    PublicResponseOut, PublicResponseOutput, PublicResponseRequest, PublicResponseUsage,
    SessionOut, TokenEstimateResponse, TokenResponse, UserCreate, UserOut, UserStats, UserUpdate,
)
from token_counter import estimate_message_tokens, estimate_text_tokens

try:
    import docker as _docker_sdk
    from docker.errors import DockerException as _DockerException, NotFound as _DockerNotFound, APIError as _DockerAPIError
    _DOCKER_SDK_AVAILABLE = True
except ImportError:
    _DOCKER_SDK_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(name)s  %(message)s")
logger = logging.getLogger("main")

# ── Configuration ─────────────────────────────────────────────────────────────
LITELLM_BASE_URL   = os.getenv("LITELLM_BASE_URL", "http://litellm:4000")
LITELLM_MASTER_KEY = os.getenv("LITELLM_MASTER_KEY", "")
ADMIN_EMAIL        = os.getenv("ADMIN_EMAIL", "admin@sentinelone.com")
ADMIN_PASSWORD     = os.getenv("ADMIN_PASSWORD", "ChangeMe!")
DEFAULT_DAILY_LIMIT = int(os.getenv("DEFAULT_DAILY_LIMIT", "50")) or None
PUBLIC_API_ENABLED = os.getenv("PUBLIC_API_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
PUBLIC_API_MAX_PROMPT_TOKENS = int(os.getenv("PUBLIC_API_MAX_PROMPT_TOKENS", "4000"))
PUBLIC_API_MAX_OUTPUT_TOKENS = int(os.getenv("PUBLIC_API_MAX_OUTPUT_TOKENS", "600"))
PUBLIC_API_ALLOW_SYSTEM_PROMPT = os.getenv("PUBLIC_API_ALLOW_SYSTEM_PROMPT", "false").lower() in {"1", "true", "yes", "on"}
SHOW_LLM_KEY_SETTINGS = os.getenv("SHOW_LLM_KEY_SETTINGS", "false").lower() in {"1", "true", "yes", "on"}
APP_ENV = os.getenv("APP_ENV", os.getenv("ENV", "development")).lower()

# ── Email / SMTP (env-var fallbacks; DB values take precedence) ──────────────
SMTP_HOST      = os.getenv("SMTP_HOST", "")
SMTP_PORT      = int(os.getenv("SMTP_PORT", "587"))
EMAIL_USERNAME = os.getenv("EMAIL_USERNAME", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
FROM_EMAIL     = os.getenv("FROM_EMAIL", ADMIN_EMAIL)
APP_BASE_URL   = os.getenv("APP_BASE_URL", "").rstrip("/")


async def _get_email_config(db: AsyncSession) -> dict:
    """Return SMTP config merging DB values (preferred) with env-var fallbacks."""
    result = await db.execute(select(AppSetting))
    s = {row.key: row.value for row in result.scalars().all()}

    # Password: decrypt from DB if present, else fall back to env var plaintext
    enc_pwd = s.get("email_password_enc")
    if enc_pwd:
        try:
            password = decrypt(enc_pwd)
        except Exception:
            password = EMAIL_PASSWORD
    else:
        password = EMAIL_PASSWORD

    try:
        port = int(s.get("smtp_port") or SMTP_PORT)
    except (ValueError, TypeError):
        port = SMTP_PORT

    return {
        "smtp_host":      s.get("smtp_host") or SMTP_HOST,
        "smtp_port":      port,
        "email_username": s.get("email_username") or EMAIL_USERNAME,
        "email_password": password,
        "from_email":     s.get("from_email") or FROM_EMAIL,
    }


# In-memory store: email -> {code, expires_at}  (cleared after verify or expiry)
_email_verify_codes: dict[str, dict] = {}
_EMAIL_CODE_TTL = 600  # 10 minutes

# Shared LLM keys (fallback when user has no per-provider key; DB values loaded at startup)
_SHARED_LLM_KEYS = {
    "openai":      os.getenv("OPENAI_API_KEY", ""),
    "anthropic":   os.getenv("ANTHROPIC_API_KEY", ""),
    "google":      os.getenv("GOOGLE_API_KEY", ""),
    "perplexity":  os.getenv("PERPLEXITY_API_KEY", ""),
    "openrouter":  os.getenv("OPENROUTER_API_KEY", ""),
}

KNOWN_PROVIDERS = [
    {"id": "openai",     "name": "OpenAI",     "env": "OPENAI_API_KEY"},
    {"id": "anthropic",  "name": "Anthropic",  "env": "ANTHROPIC_API_KEY"},
    {"id": "google",     "name": "Google",     "env": "GOOGLE_API_KEY"},
    {"id": "perplexity", "name": "Perplexity", "env": "PERPLEXITY_API_KEY"},
    {"id": "openrouter", "name": "OpenRouter", "env": "OPENROUTER_API_KEY"},
]


def _litellm_extra(model_id: str) -> dict:
    """Return extra_body for a LiteLLM call so the stored shared key is used for this request."""
    key = _SHARED_LLM_KEYS.get(_detect_provider(model_id), "")
    return {"api_key": key} if key else {}

# ── File upload limits ────────────────────────────────────────────────────────
# Set MAX_FILE_SIZE_MB in .env to restrict upload size.
MAX_FILE_SIZE_MB    = int(os.getenv("MAX_FILE_SIZE_MB") or "10")
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
ALLOWED_TEXT_TYPES  = {"text/plain", "text/markdown", "text/csv", "application/json"}
ALLOWED_PDF_TYPE    = "application/pdf"
ALLOWED_SANITIZE_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/plain",
}
ALLOWED_SANITIZE_EXTENSIONS = (".pdf", ".docx", ".xlsx", ".txt")
SANITIZE_MAX_PER_MINUTE = int(os.getenv("SANITIZE_MAX_PER_MINUTE") or "5")
SANITIZE_MAX_CONCURRENT_PER_USER = int(os.getenv("SANITIZE_MAX_CONCURRENT_PER_USER") or "1")
_sanitize_user_timestamps: dict[int, deque[float]] = defaultdict(deque)
_sanitize_user_active: dict[int, int] = defaultdict(int)
_sanitize_guard_lock = asyncio.Lock()

# ── LiteLLM client (single OpenAI-compatible client for all providers) ────────
litellm_client = AsyncOpenAI(
    api_key=LITELLM_MASTER_KEY or "no-key",
    base_url=f"{LITELLM_BASE_URL}/v1",
)


def set_litellm_master_key(key: str) -> None:
    """Hot-swap the LiteLLM master key and rebuild the shared client in-process."""
    global LITELLM_MASTER_KEY, litellm_client
    LITELLM_MASTER_KEY = key
    litellm_client = AsyncOpenAI(
        api_key=key or "no-key",
        base_url=f"{LITELLM_BASE_URL}/v1",
    )

# ── Provider base URLs for per-user direct calls ────────────────────────────
_PROVIDER_URLS = {
    "openai":     "https://api.openai.com/v1",
    "anthropic":  "https://api.anthropic.com/v1",
    "google":     "https://generativelanguage.googleapis.com/v1beta/openai/",
    "openrouter": "https://openrouter.ai/api/v1",
}


_OLLAMA_MODEL_IDS      = {s.strip() for s in os.getenv("OLLAMA_MODEL_IDS", "").split(",") if s.strip()}
_LOCAL_OPENAI_MODEL_IDS: set[str] = set()
OLLAMA_BASE_URL        = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")
COMPOSE_PROJECT_NAME   = os.getenv("COMPOSE_PROJECT_NAME", "homegrown-ai-app-demo")


def _detect_provider(model_id: str) -> str:
    if model_id in _OLLAMA_MODEL_IDS:
        return "ollama"
    if model_id in _LOCAL_OPENAI_MODEL_IDS:
        return "local_openai"
    m = model_id.lower()
    if m.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    if m.startswith("claude-"):
        return "anthropic"
    if m.startswith("gemini-"):
        return "google"
    if m.startswith(("sonar", "r1-1776")):
        return "perplexity"
    return "openrouter"


def _model_meta(model_id: str) -> dict:
    """Return category, provider, and required key info for a model."""
    provider = _detect_provider(model_id)
    if provider == "ollama":
        return {"category": "local", "provider": "Ollama", "requires_key": None}
    if provider == "local_openai":
        return {"category": "local", "provider": "Local OpenAI", "requires_key": None}
    is_free = model_id.lower().endswith(":free")
    return {
        "category": "free" if is_free else "paid",
        "provider": {"openai": "OpenAI", "anthropic": "Anthropic", "google": "Google", "perplexity": "Perplexity", "openrouter": "OpenRouter"}[provider],
        "requires_key": "openrouter" if is_free else provider,
    }


def _get_llm_key(user: User, model_id: str) -> str:
    """Returns the best available API key for model_id: per-user → shared .env → empty."""
    provider = _detect_provider(model_id)
    if user.llm_api_keys_enc:
        try:
            keys = json.loads(decrypt(user.llm_api_keys_enc))
            if keys.get(provider):
                return keys[provider]
        except Exception:
            pass
    return _SHARED_LLM_KEYS.get(provider, "")


def _user_llm_client(user: User, model_id: str):
    """Returns (AsyncOpenAI client, model_id) — uses per-user key if set, else shared LiteLLM."""
    if not user.llm_api_keys_enc:
        return litellm_client, model_id
    try:
        keys = json.loads(decrypt(user.llm_api_keys_enc))
    except Exception:
        return litellm_client, model_id
    provider = _detect_provider(model_id)
    key = keys.get(provider)
    if not key:
        return litellm_client, model_id
    base_url = _PROVIDER_URLS[provider]
    logger.info("Using per-user %s key for %s", provider, user.email)
    return AsyncOpenAI(api_key=key, base_url=base_url), model_id


# ── Fallback model list (used when LiteLLM is unreachable) ───────────────────
_FALLBACK_MODELS = [
    {"id": "gpt-4o"},
    {"id": "gpt-4o-mini"},
    {"id": "claude-sonnet-4-5-20250929"},
    {"id": "gemini-2.0-flash"},
    {"id": "gemini-1.5-pro"},
    {"id": "meta-llama/llama-3.1-8b-instruct:free"},
    {"id": "nvidia/nemotron-nano-9b-v2:free"},
    {"id": "mistralai/mistral-7b-instruct:free"},
]

# ── Cached model list ─────────────────────────────────────────────────────────
_model_cache: list[dict] = []


async def refresh_model_cache() -> list[dict]:
    global _model_cache
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            headers = {}
            if LITELLM_MASTER_KEY:
                headers["Authorization"] = f"Bearer {LITELLM_MASTER_KEY}"
            r = await client.get(f"{LITELLM_BASE_URL}/v1/models", headers=headers)
            r.raise_for_status()
            data = r.json().get("data", [])
            _model_cache = [{"id": m["id"]} for m in data]
            logger.info("LiteLLM: %d models loaded", len(_model_cache))
    except Exception as e:
        logger.warning("Could not reach LiteLLM (%s) — using fallback model list", e)
        _model_cache = []
    return _model_cache


def _validate_security_bootstrap_config() -> None:
    """Raise RuntimeError in production if insecure default secrets are detected."""
    if APP_ENV in {"dev", "development", "test", "local"}:
        return

    _insecure_secret_keys = {"dev_secret_change_me", "change_me", "changeme", "test-secret-key-for-unit-tests"}
    _insecure_admin_passwords = {"admin", "change_me", "changeme", "password", "ChangeMe!"}

    secret_key = os.getenv("SECRET_KEY", "")
    admin_password = os.getenv("ADMIN_PASSWORD", "")

    if not secret_key or secret_key in _insecure_secret_keys:
        raise RuntimeError(
            "SECRET_KEY is not set or uses an insecure default. "
            "Set a strong SECRET_KEY environment variable before running in production."
        )
    if not admin_password or admin_password in _insecure_admin_passwords:
        raise RuntimeError(
            "ADMIN_PASSWORD is not set or uses an insecure default. "
            "Set a strong ADMIN_PASSWORD environment variable before running in production."
        )


async def _acquire_sanitize_slot(user_id: int) -> None:
    now = time.time()
    async with _sanitize_guard_lock:
        timestamps = _sanitize_user_timestamps[user_id]
        while timestamps and now - timestamps[0] > 60:
            timestamps.popleft()

        if _sanitize_user_active[user_id] >= SANITIZE_MAX_CONCURRENT_PER_USER:
            raise HTTPException(status_code=429, detail="Too many concurrent sanitize requests")

        if len(timestamps) >= SANITIZE_MAX_PER_MINUTE:
            raise HTTPException(status_code=429, detail="Sanitize rate limit exceeded")

        _sanitize_user_active[user_id] += 1


async def _release_sanitize_slot(user_id: int) -> None:
    async with _sanitize_guard_lock:
        _sanitize_user_active[user_id] = max(0, _sanitize_user_active[user_id] - 1)


_SCENARIOS_FILE = os.path.join(os.path.dirname(__file__), "data", "scenarios.json")


def _load_scenarios_file() -> list[dict]:
    try:
        with open(_SCENARIOS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning("scenarios.json not found at %s — no scenarios will be seeded", _SCENARIOS_FILE)
        return []
    except Exception as e:
        logger.error("Failed to load scenarios.json: %s", e)
        return []


async def _seed_demo_scenarios(db: AsyncSession) -> None:
    count = await db.scalar(select(func.count()).select_from(DemoScenario))
    if count and count > 0:
        return
    scenarios = _load_scenarios_file()
    for d in scenarios:
        db.add(DemoScenario(
            key=d["key"], title=d["title"], category=d["category"],
            severity=d["severity"], prompt=d["prompt"],
            expected_action=d["expected_action"],
            description=d.get("description"),
            attacker_goal=d.get("attacker_goal"),
            why_caught=d.get("why_caught"),
            talking_point=d.get("talking_point"),
            entities=d.get("entities"),
            meta=d.get("meta"),
            sort_order=d.get("sort_order", 0),
        ))
    await db.commit()
    logger.info("Demo scenarios seeded from scenarios.json (%d rows)", len(scenarios))


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    _validate_security_bootstrap_config()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Schema migrations: make user_id nullable and add guest_id columns
        for sql in [
            "ALTER TABLE chat_sessions ALTER COLUMN user_id DROP NOT NULL",
            "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS guest_id VARCHAR(255)",
            "CREATE INDEX IF NOT EXISTS ix_chat_sessions_guest_id ON chat_sessions (guest_id)",
            "ALTER TABLE messages ALTER COLUMN user_id DROP NOT NULL",
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS guest_id VARCHAR(255)",
            "CREATE INDEX IF NOT EXISTS ix_messages_guest_id ON messages (guest_id)",
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS prompt_tokens INTEGER",
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS completion_tokens INTEGER",
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS total_tokens INTEGER",
            "ALTER TABLE audit_events ALTER COLUMN detail TYPE TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE",
        ]:
            try:
                await conn.execute(text(sql))
            except Exception as e:
                logger.debug("Migration skipped (%s): %s", sql[:60], e)

    async with AsyncSessionLocal() as db:
        existing = await db.scalar(select(User).where(User.role == "admin"))
        if not existing:
            import secrets as _secrets
            admin = User(
                email=f"setup-{_secrets.token_hex(8)}@wizard.internal",
                hashed_password=hash_password(_secrets.token_hex(32)),
                role="admin",
                is_active=True,
                must_change_password=True,
            )
            db.add(admin)
            await db.commit()
            logger.info("Bootstrap admin placeholder created (id=%s)", admin.id)

    async with AsyncSessionLocal() as db:
        await _seed_demo_scenarios(db)

    # Override JWT secret and LiteLLM master key from DB if saved via the admin UI
    async with AsyncSessionLocal() as db:
        row = await db.get(AppSetting, "jwt_secret_enc")
        if row:
            try:
                set_secret_key(decrypt(row.value))
                logger.info("JWT secret loaded from app_settings")
            except Exception as exc:
                logger.warning("Could not load JWT secret from DB: %s", exc)
        llm_row = await db.get(AppSetting, "litellm_key_enc")
        if llm_row:
            try:
                set_litellm_master_key(decrypt(llm_row.value))
                logger.info("LiteLLM master key loaded from app_settings")
            except Exception as exc:
                logger.warning("Could not load LiteLLM master key from DB: %s", exc)

        # Load stored provider API keys into _SHARED_LLM_KEYS (override env var values)
        for p in KNOWN_PROVIDERS:
            pk_row = await db.get(AppSetting, f"provider_key_{p['id']}")
            if pk_row:
                try:
                    _SHARED_LLM_KEYS[p["id"]] = decrypt(pk_row.value)
                except Exception:
                    pass

        # Load hot-swappable application settings
        global DEFAULT_DAILY_LIMIT, MAX_FILE_SIZE_MB, MAX_FILE_SIZE_BYTES, _OLLAMA_MODEL_IDS, OLLAMA_BASE_URL
        dl_row = await db.get(AppSetting, "daily_limit")
        if dl_row:
            DEFAULT_DAILY_LIMIT = int(dl_row.value) or None
        mf_row = await db.get(AppSetting, "max_file_mb")
        if mf_row:
            MAX_FILE_SIZE_MB = int(mf_row.value)
            MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
        om_row = await db.get(AppSetting, "ollama_model_ids")
        if om_row:
            _OLLAMA_MODEL_IDS = {s.strip() for s in om_row.value.split(",") if s.strip()}
        ob_row = await db.get(AppSetting, "ollama_base_url")
        if ob_row:
            OLLAMA_BASE_URL = ob_row.value

    await refresh_model_cache()
    yield


app = FastAPI(title="AI Chat + Prompt Security v2", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ── HTML routes ───────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(open("static/index.html").read())

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return HTMLResponse(open("static/login.html").read())

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    """Serve admin UI. No server-side gate — auth is enforced by the JS overlay
    and by require_admin on every admin API endpoint."""
    return HTMLResponse(open("static/admin.html").read())


# ── Auth ──────────────────────────────────────────────────────────────────────
@app.post("/auth/login", response_model=TokenResponse)
async def login(body: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(User).where(User.email == body.email, User.is_active == True)
        .options(selectinload(User.ps_tenant))
    )
    user = result.scalar_one_or_none()
    if not user or not user.hashed_password or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Detect first-ever login by checking for prior login events
    prior_logins = await db.scalar(
        select(func.count(AuditEvent.id))
        .where(AuditEvent.user_id == user.id, AuditEvent.event_type == "user_login")
    )
    if prior_logins == 0:
        await _log_audit(db, user.id, user.email, "user_first_login", "First sign-in")
    await _log_audit(db, user.id, user.email, "user_login", None)

    token = create_access_token({"sub": str(user.id)})
    # httpOnly cookie used for server-side page-level auth (e.g. GET /admin)
    response.set_cookie(
        "hgapp_session", token,
        httponly=True, samesite="lax", secure=False,
        max_age=TOKEN_TTL_H * 3600,
    )
    return TokenResponse(
        access_token=token,
        user=_user_out(user),
    )


@app.post("/auth/admin-login", response_model=TokenResponse)
async def admin_login(body: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)):
    """Password-only admin login. Checks against the stored admin_password_hash (falls back to ADMIN_PASSWORD env var)."""
    # Find the single admin user
    result = await db.execute(
        select(User)
        .where(User.role == "admin", User.is_active == True)
        .options(selectinload(User.ps_tenant))
    )
    admin_user = result.scalar_one_or_none()
    if not admin_user:
        raise HTTPException(status_code=401, detail="Invalid password")

    # Check against AppSetting hash first, fall back to env var
    pw_row = await db.get(AppSetting, "admin_password_hash")
    if pw_row:
        if not verify_password(body.password, pw_row.value):
            raise HTTPException(status_code=401, detail="Invalid password")
    else:
        if body.password != ADMIN_PASSWORD:
            raise HTTPException(status_code=401, detail="Invalid password")

    await _log_audit(db, admin_user.id, admin_user.email, "user_login", "Admin password-only login")
    await db.commit()

    token = create_access_token({"sub": str(admin_user.id)})
    response.set_cookie(
        "hgapp_session", token,
        httponly=True, samesite="lax", secure=False,
        max_age=TOKEN_TTL_H * 3600,
    )
    return TokenResponse(access_token=token, user=_user_out(admin_user))


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str


@app.post("/auth/change-password")
async def change_password(
    body: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user.hashed_password or not verify_password(body.current_password, current_user.hashed_password):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    if body.new_password != body.confirm_password:
        raise HTTPException(status_code=422, detail="Passwords do not match")
    if len(body.new_password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")
    if body.new_password == body.current_password:
        raise HTTPException(status_code=422, detail="New password must differ from the current password")

    current_user.hashed_password = hash_password(body.new_password)
    current_user.must_change_password = False
    await db.commit()
    await _log_audit(db, current_user.id, current_user.email, "password_changed", "User changed password on first login")
    await db.commit()
    return {"ok": True}


@app.post("/auth/logout", status_code=204)
async def logout(response: Response):
    """Clear the server-side session cookie."""
    response.delete_cookie("hgapp_session", samesite="lax")
    return

@app.get("/auth/me", response_model=UserOut)
async def me(current_user: User = Depends(get_current_user)):
    return _user_out(current_user)


def _setup_complete(s: dict) -> bool:
    """Return True only when all required wizard items are configured."""
    any_llm = any(s.get(f"provider_key_{p['id']}") for p in KNOWN_PROVIDERS)
    return bool(
        s.get("admin_password_hash")
        and s.get("jwt_secret_enc")
        and encryption_key_overridden()
        and any_llm
    )


@app.get("/setup/status")
async def setup_status(db: AsyncSession = Depends(get_db)):
    """No-auth endpoint. Returns whether initial setup is still needed and if default credentials are still active."""
    result = await db.execute(select(AppSetting))
    s = {row.key: row.value for row in result.scalars().all()}
    admin = await db.scalar(select(User).where(User.role == "admin", User.is_active == True))
    default_credentials = bool(admin and admin.must_change_password)
    return {"needs_setup": not _setup_complete(s), "default_credentials": default_credentials}


@app.post("/setup/bootstrap-token")
async def setup_bootstrap_token(db: AsyncSession = Depends(get_db)):
    """No-auth endpoint. Issues a short-lived admin JWT for the Setup Wizard.
    Remains available until all required wizard items are configured.
    Returns 403 once setup is complete."""
    result = await db.execute(select(AppSetting))
    s = {row.key: row.value for row in result.scalars().all()}
    if _setup_complete(s):
        raise HTTPException(status_code=403, detail="Setup already complete")
    admin = await db.scalar(select(User).where(User.role == "admin", User.is_active == True))
    if not admin:
        raise HTTPException(status_code=500, detail="No admin user found")
    token = create_access_token({"sub": str(admin.id)}, expires_h=2)
    return {"access_token": token}


@app.get("/users/me/api-keys", response_model=list[APIKeyOut])
async def list_my_api_keys(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(APIKey)
        .where(APIKey.user_id == current_user.id)
        .order_by(desc(APIKey.created_at))
    )
    return [_api_key_out(k) for k in result.scalars().all()]


@app.post("/users/me/api-keys", response_model=APIKeyCreateResponse, status_code=201)
async def create_my_api_key(
    body: APIKeyCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Key name is required")

    raw_key = create_api_key()
    prefix = raw_key[:16]
    record = APIKey(
        user_id=current_user.id,
        name=name,
        key_prefix=prefix,
        key_hash=hash_api_key(raw_key),
        is_active=True,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    await _log_audit(db, current_user.id, current_user.email, "api_key_created", f"name={name}")
    return APIKeyCreateResponse(api_key=raw_key, key=_api_key_out(record))


@app.delete("/users/me/api-keys/{key_id}", status_code=204)
async def delete_my_api_key(
    key_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    key = await db.scalar(
        select(APIKey).where(APIKey.id == key_id, APIKey.user_id == current_user.id)
    )
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")
    await db.delete(key)
    await db.commit()
    await _log_audit(db, current_user.id, current_user.email, "api_key_deleted", f"id={key_id} name={key.name}")


# ── Health + Models ───────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "litellm_url": LITELLM_BASE_URL,
        "models_loaded": len(_model_cache),
    }


_APP_VERSION: str = "unknown"

def _load_app_version() -> str:
    """Read VERSION file from repo root (one level above app/)."""
    for candidate in [
        Path(__file__).parent.parent / "VERSION",  # repo root when running in container
        Path(__file__).parent / "VERSION",          # fallback if copied alongside app
    ]:
        if candidate.exists():
            return candidate.read_text().strip()
    return "unknown"

_APP_VERSION = _load_app_version()


@app.get("/version")
async def version():
    return {"version": _APP_VERSION}

@app.get("/models")
async def models(current_user: User = Depends(get_current_user)):
    live = _model_cache or await refresh_model_cache()
    available = live if live else _FALLBACK_MODELS
    allowed = current_user.allowed_models
    if allowed is not None:
        available = [m for m in available if m["id"] in allowed]

    # Build set of providers that have a usable key (user-level or shared)
    user_providers: set[str] = set()
    if current_user.llm_api_keys_enc:
        try:
            user_providers = {k for k, v in json.loads(decrypt(current_user.llm_api_keys_enc)).items() if v}
        except Exception:
            pass
    shared_providers = {k for k, v in _SHARED_LLM_KEYS.items() if v}

    enriched = []
    for m in available:
        meta = _model_meta(m["id"])
        provider = _detect_provider(m["id"])
        key_set = provider == "ollama" or provider in user_providers or provider in shared_providers
        enriched.append({**m, **meta, "key_set": key_set})

    return {"models": enriched, "fallback": not bool(live)}


@app.post("/admin/refresh-models")
async def admin_refresh_models(admin: User = Depends(require_admin)):
    updated = await refresh_model_cache()
    return {"models_loaded": len(updated), "fallback": not bool(updated)}


@app.post("/chat/token-estimate", response_model=TokenEstimateResponse)
async def chat_token_estimate(
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
):
    available = _model_cache or _FALLBACK_MODELS
    model = request.model or available[0]["id"]
    if not model:
        raise HTTPException(status_code=503, detail="No models available — check LiteLLM config")

    if current_user.allowed_models is not None and model not in current_user.allowed_models:
        raise HTTPException(status_code=403, detail="Model not allowed for this user")

    system_prompt = request.system_prompt or "You are a helpful AI assistant."
    payload = [{"role": "system", "content": system_prompt}] + [
        {"role": m.role, "content": _build_content(m)} for m in request.messages
    ]
    return TokenEstimateResponse(
        estimated_prompt_tokens=estimate_message_tokens(payload, model=model),
        model=model,
    )


@app.post("/v1/responses", response_model=PublicResponseOut)
async def public_responses(
    body: PublicResponseRequest,
    auth_ctx: tuple[APIKey, User] = Depends(get_current_api_key),
    db: AsyncSession = Depends(get_db),
):
    _ensure_public_api_enabled()
    api_key, current_user = auth_ctx

    available = _model_cache or await refresh_model_cache() or _FALLBACK_MODELS
    model = body.model or available[0]["id"]
    if not model:
        raise HTTPException(status_code=503, detail="No models available — check LiteLLM config")
    if current_user.allowed_models is not None and model not in current_user.allowed_models:
        raise HTTPException(status_code=403, detail="Model not allowed for this API key")

    user_prompt = (body.input or "").strip()
    if not user_prompt:
        raise HTTPException(status_code=400, detail="input is required")

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    used_today = await db.scalar(
        select(func.count(Message.id)).where(
            Message.user_id == current_user.id,
            Message.role == "user",
            Message.created_at >= today_start,
        )
    ) or 0
    if current_user.daily_message_limit and used_today >= current_user.daily_message_limit:
        raise HTTPException(status_code=429, detail="Daily message limit reached")

    system_prompt = "You are a helpful AI assistant."
    if PUBLIC_API_ALLOW_SYSTEM_PROMPT and body.system_prompt:
        system_prompt = body.system_prompt

    payload = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    prompt_tokens = estimate_message_tokens(payload, model=model)
    if prompt_tokens > PUBLIC_API_MAX_PROMPT_TOKENS:
        raise HTTPException(
            status_code=413,
            detail=f"Prompt too large for public test endpoint ({prompt_tokens} > {PUBLIC_API_MAX_PROMPT_TOKENS} tokens).",
        )

    ps_client = _build_ps_api_client(current_user)
    prompt_action = "pass"
    ps_violations: list = []
    effective_prompt = user_prompt
    if ps_client:
        try:
            ps_result = await ps_client.protect_prompt(
                user_prompt=user_prompt,
                system_prompt=system_prompt,
                user=current_user.email,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Prompt Security scan failed: {exc}")
        if not ps_result.allowed:
            raise HTTPException(status_code=403, detail={"ps_action": "block", "violations": ps_result.violations})
        if ps_result.modified_text:
            effective_prompt = ps_result.modified_text
            prompt_action = "modify"
            payload[-1]["content"] = effective_prompt

    llm, effective_model = _user_llm_client(current_user, model)
    try:
        _pub_kwargs: dict = {"model": effective_model, "messages": payload, "max_tokens": PUBLIC_API_MAX_OUTPUT_TOKENS}
        if llm is litellm_client:
            _pub_kwargs["extra_body"] = _litellm_extra(effective_model)
        resp = await llm.chat.completions.create(**_pub_kwargs)
    except Exception as exc:
        logger.error("Public API completion error: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))

    text = _extract_response_text(resp)
    if not text:
        raise HTTPException(status_code=502, detail="Model returned an empty response")

    if ps_client:
        try:
            ps_resp = await ps_client.protect_response(
                response_text=text,
                user_prompt=user_prompt,
                system_prompt=system_prompt,
                user=current_user.email,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Prompt Security response scan failed: {exc}")
        ps_violations = ps_resp.violations
        if not ps_resp.allowed:
            raise HTTPException(status_code=403, detail={"ps_action": "block", "violations": ps_violations})
        if ps_resp.modified_text:
            text = ps_resp.modified_text
            prompt_action = "modify"

    usage = getattr(resp, "usage", None)
    prompt_tokens = int(getattr(usage, "prompt_tokens", prompt_tokens) or prompt_tokens)
    completion_tokens = int(getattr(usage, "completion_tokens", estimate_text_tokens(text, model=effective_model)))
    total_tokens = int(getattr(usage, "total_tokens", prompt_tokens + completion_tokens))

    session = ChatSession(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        title=f"API Test: {user_prompt[:48]}" + ("…" if len(user_prompt) > 48 else ""),
    )
    db.add(session)
    await db.commit()
    await _log_msg(db, session.id, current_user.id, "user", user_prompt, model)
    await _log_msg(
        db, session.id, current_user.id, "assistant", text, model,
        ps_scanned=bool(ps_client), ps_action=prompt_action, ps_violations=ps_violations,
    )
    await _log_audit(
        db, current_user.id, current_user.email, "public_api_invoked",
        f"model={model} key={api_key.name} total_tokens={total_tokens}",
    )

    return PublicResponseOut(
        id=f"resp_{uuid.uuid4().hex}",
        model=model,
        output=[PublicResponseOutput(text=text)],
        usage=PublicResponseUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        ),
        ps_scanned=bool(ps_client),
        ps_action=prompt_action,
        ps_violations=ps_violations,
    )


# ── User self-service ─────────────────────────────────────────────────────────
@app.get("/users/me/stats", response_model=UserStats)
async def my_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    count = await db.scalar(
        select(func.count(Message.id)).where(
            Message.user_id == current_user.id,
            Message.role == "user",
            Message.created_at >= today_start,
        )
    ) or 0
    return UserStats(messages_today=count, daily_limit=current_user.daily_message_limit)


@app.patch("/users/me/ps-config", response_model=UserOut)
async def update_my_ps_config(
    body: PSConfigUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.ps_tenant_id is not None:
        tenant = await db.get(PSTenant, body.ps_tenant_id)
        if not tenant:
            raise HTTPException(status_code=404, detail="PS Tenant not found")
        if current_user.ps_tenant_id != body.ps_tenant_id and body.ps_api_key is None:
            # Tenant changed without a new App ID — clear the stored key so the
            # old tenant's App ID is not silently used against the new endpoint.
            current_user.ps_api_key_enc = None
        current_user.ps_tenant_id = body.ps_tenant_id

    if body.ps_api_key is not None:
        current_user.ps_api_key_enc = encrypt(body.ps_api_key) if body.ps_api_key else None

    if body.ps_mode in ("api", "gateway"):
        current_user.ps_mode = body.ps_mode

    if body.ps_enabled is not None:
        current_user.ps_enabled = body.ps_enabled

    await db.commit()
    await db.refresh(current_user, ["ps_tenant"])
    parts = []
    if body.ps_api_key is not None: parts.append("App ID updated")
    if body.ps_mode in ("api", "gateway"): parts.append(f"mode={body.ps_mode}")
    if body.ps_enabled is not None: parts.append(f"enabled={body.ps_enabled}")
    if body.ps_tenant_id is not None: parts.append(f"tenant_id={body.ps_tenant_id}")
    await _log_audit(db, current_user.id, current_user.email, "ps_config_changed", "; ".join(parts) or None)
    return _user_out(current_user)


# ── Admin: Users ──────────────────────────────────────────────────────────────
@app.get("/admin/users", response_model=list[UserOut])
async def admin_list_users(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(User).where(User.role != "admin").options(selectinload(User.ps_tenant)).order_by(User.created_at)
    )
    return [_user_out(u) for u in result.scalars().all()]


@app.post("/admin/users", response_model=UserOut, status_code=201)
async def admin_create_user(
    body: UserCreate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    existing = await db.scalar(select(User).where(User.email == body.email))
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")
    user = User(
        email=body.email,
        hashed_password=hash_password(body.password),
        role=body.role,
        daily_message_limit=body.daily_message_limit if body.daily_message_limit is not None else DEFAULT_DAILY_LIMIT,
        allowed_models=body.allowed_models,
        ps_tenant_id=body.ps_tenant_id,
        must_change_password=body.must_change_password,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user, ["ps_tenant"])
    await _log_audit(db, admin.id, admin.email, "user_created", f"email={user.email} role={user.role} must_change_password={body.must_change_password}")
    return _user_out(user)


@app.get("/admin/users/{user_id}", response_model=UserOut)
async def admin_get_user(
    user_id: int,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(User).where(User.id == user_id).options(selectinload(User.ps_tenant))
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return _user_out(user)


@app.patch("/admin/users/{user_id}", response_model=UserOut)
async def admin_update_user(
    user_id: int,
    body: UserUpdate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(User).where(User.id == user_id).options(selectinload(User.ps_tenant))
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if body.email is not None:
        user.email = body.email
    if body.password is not None:
        user.hashed_password = hash_password(body.password)
    if body.role is not None:
        user.role = body.role
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.daily_message_limit is not None:
        user.daily_message_limit = body.daily_message_limit
    if body.allowed_models is not None:
        user.allowed_models = body.allowed_models
    if body.ps_tenant_id is not None:
        user.ps_tenant_id = body.ps_tenant_id
    if body.ps_enabled is not None:
        user.ps_enabled = body.ps_enabled
    if body.must_change_password is not None:
        user.must_change_password = body.must_change_password

    await db.commit()
    await db.refresh(user, ["ps_tenant"])
    changed = [k for k in ("email","role","is_active","ps_enabled","ps_tenant_id","must_change_password") if getattr(body,k,None) is not None]
    await _log_audit(db, admin.id, admin.email, "user_updated", f"{user.email}: {', '.join(changed)}")
    return _user_out(user)


@app.delete("/admin/users/{user_id}", status_code=204)
async def admin_delete_user(
    user_id: int,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    email = user.email
    await db.delete(user)
    await db.commit()
    await _log_audit(db, admin.id, admin.email, "user_deleted", f"email={email}")


# ── Admin: PS Tenants ─────────────────────────────────────────────────────────
@app.get("/admin/ps-tenants", response_model=list[PSTenantOut])
async def admin_list_ps_tenants(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(PSTenant).order_by(PSTenant.name))
    return result.scalars().all()


@app.post("/admin/ps-tenants", response_model=PSTenantOut, status_code=201)
async def admin_create_ps_tenant(
    body: PSTenantCreate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    existing = await db.scalar(select(PSTenant).where(PSTenant.name == body.name))
    if existing:
        raise HTTPException(status_code=409, detail="Tenant name already exists")
    tenant = PSTenant(name=body.name, base_url=body.base_url, gateway_url=body.gateway_url)
    db.add(tenant)
    await db.commit()
    await db.refresh(tenant)
    await _log_audit(db, admin.id, admin.email, "tenant_created", f"{tenant.name} — {tenant.base_url}")
    return tenant


@app.patch("/admin/ps-tenants/{tenant_id}", response_model=PSTenantOut)
async def admin_update_ps_tenant(
    tenant_id: int,
    body: PSTenantUpdate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    tenant = await db.get(PSTenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    if body.name is not None:
        tenant.name = body.name
    if body.base_url is not None:
        tenant.base_url = body.base_url
    if body.gateway_url is not None:
        tenant.gateway_url = body.gateway_url
    await db.commit()
    await db.refresh(tenant)
    await _log_audit(db, admin.id, admin.email, "tenant_updated", f"{tenant.name}")
    return tenant


@app.delete("/admin/ps-tenants/{tenant_id}", status_code=204)
async def admin_delete_ps_tenant(
    tenant_id: int,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    tenant = await db.get(PSTenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    name = tenant.name
    await db.delete(tenant)
    await db.commit()
    await _log_audit(db, admin.id, admin.email, "tenant_deleted", f"name={name}")


# ── Admin: PS Tenants (non-admin read — for settings dropdown) ────────────────
@app.get("/ps-tenants", response_model=list[PSTenantOut])
async def list_ps_tenants_public(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(PSTenant).order_by(PSTenant.name))
    return result.scalars().all()


# ── Admin: Stats ──────────────────────────────────────────────────────────────
@app.get("/admin/stats")
async def admin_stats(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    total_messages = await db.scalar(select(func.count(Message.id))) or 0
    messages_today = await db.scalar(
        select(func.count(Message.id)).where(Message.created_at >= today_start)
    ) or 0
    total_users = await db.scalar(select(func.count(User.id))) or 0
    total_sessions = await db.scalar(select(func.count(ChatSession.id))) or 0

    # Active users today
    active_result = await db.execute(
        select(func.count(func.distinct(Message.user_id)))
        .where(Message.created_at >= today_start)
    )
    active_users_today = active_result.scalar() or 0

    # PS actions distribution
    ps_actions_result = await db.execute(
        select(Message.ps_action, func.count(Message.id))
        .where(Message.ps_scanned == True)
        .group_by(Message.ps_action)
    )
    ps_actions = {row[0]: row[1] for row in ps_actions_result.all() if row[0]}
    ps_scanned = sum(ps_actions.values())
    ps_blocked  = ps_actions.get("block", 0)

    # Messages by day (last 14 days)
    from sqlalchemy import cast, Date as SADate
    days_result = await db.execute(
        select(cast(Message.created_at, SADate).label("day"), func.count(Message.id))
        .group_by("day")
        .order_by("day")
        .limit(14)
    )
    messages_by_day = [{"date": str(r[0]), "count": r[1]} for r in days_result.all()]

    # Messages by model
    model_result = await db.execute(
        select(Message.model, func.count(Message.id))
        .where(Message.model.isnot(None))
        .group_by(Message.model)
        .order_by(desc(func.count(Message.id)))
        .limit(8)
    )
    messages_by_model = {r[0]: r[1] for r in model_result.all()}

    # Top users (all time)
    top_users_result = await db.execute(
        select(User.id, User.email, func.count(Message.id).label("count"))
        .join(Message, Message.user_id == User.id)
        .where(Message.role == "user")
        .group_by(User.id, User.email)
        .order_by(desc("count"))
        .limit(10)
    )
    top_users = [{"id": r[0], "email": r[1], "message_count": r[2]} for r in top_users_result.all()]

    # Per-user message counts today
    user_counts_result = await db.execute(
        select(User.email, func.count(Message.id).label("count"))
        .join(Message, Message.user_id == User.id)
        .where(Message.created_at >= today_start, Message.role == "user")
        .group_by(User.email)
        .order_by(desc("count"))
    )
    user_counts = [{"email": r[0], "count": r[1]} for r in user_counts_result.all()]

    # Token usage totals
    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total_tokens_all = await db.scalar(
        select(func.coalesce(func.sum(Message.total_tokens), 0))
        .where(Message.role == "assistant")
    ) or 0
    total_tokens_month = await db.scalar(
        select(func.coalesce(func.sum(Message.total_tokens), 0))
        .where(Message.role == "assistant", Message.created_at >= month_start)
    ) or 0
    total_tokens_today = await db.scalar(
        select(func.coalesce(func.sum(Message.total_tokens), 0))
        .where(Message.role == "assistant", Message.created_at >= today_start)
    ) or 0

    return {
        "total_messages": total_messages,
        "messages_today": messages_today,
        "total_users": total_users,
        "total_sessions": total_sessions,
        "active_users_today": active_users_today,
        "ps_actions": ps_actions,
        "ps_scanned": ps_scanned,
        "ps_blocked": ps_blocked,
        "messages_by_day": messages_by_day,
        "messages_by_model": messages_by_model,
        "top_users": top_users,
        "user_counts_today": user_counts,
        "total_tokens_all": int(total_tokens_all),
        "total_tokens_month": int(total_tokens_month),
        "total_tokens_today": int(total_tokens_today),
    }


@app.get("/admin/guest-activity")
async def admin_guest_activity(
    identifier: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    # Return sessions with their messages for rich chat history
    sessions_result = await db.execute(
        select(ChatSession)
        .where(ChatSession.guest_id == identifier)
        .order_by(desc(ChatSession.created_at))
        .limit(200)
    )
    sessions = sessions_result.scalars().all()
    session_ids = [s.id for s in sessions]
    session_map = {s.id: s for s in sessions}

    if session_ids:
        msgs_result = await db.execute(
            select(Message)
            .where(Message.session_id.in_(session_ids))
            .order_by(Message.session_id, Message.id)
        )
        messages = msgs_result.scalars().all()
    else:
        messages = []

    # Group messages by session
    from collections import defaultdict
    by_session: dict = defaultdict(list)
    for m in messages:
        by_session[m.session_id].append(m)

    result = []
    for sid in session_ids:
        s = session_map[sid]
        result.append({
            "session_id": sid,
            "title": s.title,
            "created_at": s.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "messages": [
                {
                    "id": m.id,
                    "role": m.role,
                    "content": m.content,
                    "model": m.model,
                    "ps_scanned": m.ps_scanned,
                    "ps_action": m.ps_action,
                    "ps_violations": m.ps_violations or [],
                    "prompt_tokens": m.prompt_tokens,
                    "completion_tokens": m.completion_tokens,
                    "total_tokens": m.total_tokens,
                    "created_at": m.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                }
                for m in by_session[sid]
            ],
        })
    return result


@app.get("/admin/users/{user_id}/chat-history")
async def admin_user_chat_history(
    user_id: int,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    sessions_result = await db.execute(
        select(ChatSession)
        .where(ChatSession.user_id == user_id)
        .order_by(desc(ChatSession.created_at))
        .limit(200)
    )
    sessions = sessions_result.scalars().all()
    session_ids = [s.id for s in sessions]
    session_map = {s.id: s for s in sessions}

    if session_ids:
        msgs_result = await db.execute(
            select(Message)
            .where(Message.session_id.in_(session_ids))
            .order_by(Message.session_id, Message.id)
        )
        messages = msgs_result.scalars().all()
    else:
        messages = []

    from collections import defaultdict
    by_session: dict = defaultdict(list)
    for m in messages:
        by_session[m.session_id].append(m)

    result = []
    for sid in session_ids:
        s = session_map[sid]
        result.append({
            "session_id": sid,
            "title": s.title,
            "created_at": s.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "messages": [
                {
                    "id": m.id,
                    "role": m.role,
                    "content": m.content,
                    "model": m.model,
                    "ps_scanned": m.ps_scanned,
                    "ps_action": m.ps_action,
                    "ps_violations": m.ps_violations or [],
                    "prompt_tokens": m.prompt_tokens,
                    "completion_tokens": m.completion_tokens,
                    "total_tokens": m.total_tokens,
                    "created_at": m.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                }
                for m in by_session[sid]
            ],
        })
    return result


@app.get("/admin/guest-stats")
async def admin_guest_stats(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import cast, Date as SADate
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    total_events = await db.scalar(
        select(func.count(AuditEvent.id)).where(AuditEvent.event_type == "guest_chat")
    ) or 0
    events_today = await db.scalar(
        select(func.count(AuditEvent.id))
        .where(AuditEvent.event_type == "guest_chat", AuditEvent.created_at >= today_start)
    ) or 0
    total_guests = await db.scalar(
        select(func.count(func.distinct(AuditEvent.user_email)))
        .where(AuditEvent.event_type == "guest_chat")
    ) or 0
    guests_today = await db.scalar(
        select(func.count(func.distinct(AuditEvent.user_email)))
        .where(AuditEvent.event_type == "guest_chat", AuditEvent.created_at >= today_start)
    ) or 0

    # Aggregate token totals across all guest messages
    total_tokens_all = await db.scalar(
        select(func.coalesce(func.sum(Message.total_tokens), 0))
        .where(Message.guest_id.isnot(None), Message.role == "assistant")
    ) or 0
    total_tokens_month = await db.scalar(
        select(func.coalesce(func.sum(Message.total_tokens), 0))
        .where(Message.guest_id.isnot(None), Message.role == "assistant",
               Message.created_at >= month_start)
    ) or 0

    days_result = await db.execute(
        select(cast(AuditEvent.created_at, SADate).label("day"), func.count(AuditEvent.id))
        .where(AuditEvent.event_type == "guest_chat")
        .group_by("day")
        .order_by("day")
        .limit(14)
    )
    events_by_day = [{"date": str(r[0]), "count": r[1]} for r in days_result.all()]

    top_guests_result = await db.execute(
        select(AuditEvent.user_email, func.count(AuditEvent.id).label("count"))
        .where(AuditEvent.event_type == "guest_chat")
        .group_by(AuditEvent.user_email)
        .order_by(desc("count"))
        .limit(50)
    )
    top_guests = [{"identifier": r[0], "count": r[1]} for r in top_guests_result.all()]

    last_seen_result = await db.execute(
        select(AuditEvent.user_email, func.max(AuditEvent.created_at).label("last_seen"))
        .where(AuditEvent.event_type == "guest_chat")
        .group_by(AuditEvent.user_email)
    )
    last_seen_map = {r[0]: r[1].strftime("%Y-%m-%d %H:%M") for r in last_seen_result.all()}

    # Per-guest token totals (all-time and this month)
    tokens_all_result = await db.execute(
        select(Message.guest_id, func.coalesce(func.sum(Message.total_tokens), 0))
        .where(Message.guest_id.isnot(None), Message.role == "assistant")
        .group_by(Message.guest_id)
    )
    tokens_all_map = {r[0]: int(r[1]) for r in tokens_all_result.all()}

    tokens_month_result = await db.execute(
        select(Message.guest_id, func.coalesce(func.sum(Message.total_tokens), 0))
        .where(Message.guest_id.isnot(None), Message.role == "assistant",
               Message.created_at >= month_start)
        .group_by(Message.guest_id)
    )
    tokens_month_map = {r[0]: int(r[1]) for r in tokens_month_result.all()}

    for g in top_guests:
        g["last_seen"] = last_seen_map.get(g["identifier"], "—")
        g["tokens_total"] = tokens_all_map.get(g["identifier"], 0)
        g["tokens_month"] = tokens_month_map.get(g["identifier"], 0)

    return {
        "total_guests": total_guests,
        "guests_today": guests_today,
        "total_events": total_events,
        "events_today": events_today,
        "total_tokens_all": int(total_tokens_all),
        "total_tokens_month": int(total_tokens_month),
        "events_by_day": events_by_day,
        "top_guests": top_guests,
    }


@app.get("/admin/users/{user_id}/stats")
async def admin_user_stats(
    user_id: int,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import cast, Date as SADate
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    total = await db.scalar(select(func.count(Message.id)).where(Message.user_id == user_id)) or 0
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today = await db.scalar(
        select(func.count(Message.id)).where(Message.user_id == user_id, Message.created_at >= today_start)
    ) or 0
    sessions_count = await db.scalar(
        select(func.count(ChatSession.id)).where(ChatSession.user_id == user_id)
    ) or 0
    ps_blocked = await db.scalar(
        select(func.count(Message.id)).where(Message.user_id == user_id, Message.ps_action == "block")
    ) or 0

    days_result = await db.execute(
        select(cast(Message.created_at, SADate).label("day"), func.count(Message.id))
        .where(Message.user_id == user_id)
        .group_by("day").order_by("day").limit(14)
    )
    messages_by_day = [{"date": str(r[0]), "count": r[1]} for r in days_result.all()]

    model_result = await db.execute(
        select(Message.model, func.count(Message.id))
        .where(Message.user_id == user_id, Message.model.isnot(None))
        .group_by(Message.model).order_by(desc(func.count(Message.id))).limit(6)
    )
    messages_by_model = {r[0]: r[1] for r in model_result.all()}

    recent_result = await db.execute(
        select(Message).where(Message.user_id == user_id).order_by(desc(Message.id)).limit(20)
    )
    recent = [
        {
            "role": m.role,
            "content": m.content or "",
            "model": m.model,
            "ps_scanned": m.ps_scanned,
            "ps_action": m.ps_action,
            "prompt_tokens": m.prompt_tokens,
            "completion_tokens": m.completion_tokens,
            "total_tokens": m.total_tokens,
            "created_at": m.created_at.isoformat(),
        }
        for m in recent_result.scalars().all()
    ]

    return {
        "total_messages": total, "messages_today": today,
        "total_sessions": sessions_count, "ps_blocked": ps_blocked,
        "messages_by_day": messages_by_day, "messages_by_model": messages_by_model,
        "recent_messages": recent,
    }


# ── Sessions (chat history) ───────────────────────────────────────────────────
@app.get("/sessions", response_model=list[SessionOut])
async def list_sessions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ChatSession)
        .where(ChatSession.user_id == current_user.id)
        .order_by(desc(ChatSession.created_at))
        .limit(50)
    )
    return result.scalars().all()


@app.get("/sessions/{session_id}/messages", response_model=list[MessageOut])
async def get_session_messages(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    session = await db.scalar(
        select(ChatSession).where(
            ChatSession.id == session_id,
            ChatSession.user_id == current_user.id,
        )
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    result = await db.execute(
        select(Message).where(Message.session_id == session_id).order_by(Message.id)
    )
    return result.scalars().all()


@app.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    session = await db.scalar(
        select(ChatSession).where(
            ChatSession.id == session_id,
            ChatSession.user_id == current_user.id,
        )
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    await db.delete(session)
    await db.commit()


@app.delete("/sessions", status_code=204)
async def delete_all_sessions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete all chat sessions for the current user."""
    result = await db.execute(
        select(ChatSession).where(ChatSession.user_id == current_user.id)
    )
    for session in result.scalars().all():
        await db.delete(session)
    await db.commit()


async def _read_upload_with_limit(file: UploadFile) -> bytes:
    data = await file.read(MAX_FILE_SIZE_BYTES + 1)
    if len(data) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large. Max {MAX_FILE_SIZE_MB} MB.")
    return data


# ── File upload ───────────────────────────────────────────────────────────────
@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    data = await _read_upload_with_limit(file)

    mime = (file.content_type or "application/octet-stream").split(";")[0].strip()
    filename = file.filename or "upload"

    if mime in ALLOWED_IMAGE_TYPES:
        b64 = base64.b64encode(data).decode()
        return {"type": "image", "content": f"data:{mime};base64,{b64}", "filename": filename, "size_bytes": len(data)}

    if mime == ALLOWED_PDF_TYPE or filename.lower().endswith(".pdf"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            text = "\n\n".join(page.extract_text() or "" for page in reader.pages).strip()
            if not text:
                raise HTTPException(status_code=422, detail="Could not extract text from PDF.")
        except ImportError:
            raise HTTPException(status_code=501, detail="PDF support not installed.")
        return {"type": "text", "content": text, "filename": filename, "size_bytes": len(data)}

    if mime in ALLOWED_TEXT_TYPES or filename.lower().endswith((".txt", ".md", ".csv", ".json")):
        text = data.decode("utf-8", errors="replace")
        return {"type": "text", "content": text, "filename": filename, "size_bytes": len(data)}

    raise HTTPException(status_code=415, detail=f"Unsupported file type '{mime}'.")


# ── Chat streaming ────────────────────────────────────────────────────────────
@app.post("/chat/stream")
async def chat_stream(
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not request.messages:
        raise HTTPException(status_code=400, detail="messages list cannot be empty")

    available = _model_cache or _FALLBACK_MODELS
    model = request.model or available[0]["id"]
    if not model:
        raise HTTPException(status_code=503, detail="No models available — check LiteLLM config")
    if current_user.allowed_models is not None and model not in current_user.allowed_models:
        raise HTTPException(status_code=403, detail="Model not allowed for this user")

    last_user_msg = next((m.content for m in reversed(request.messages) if m.role == "user"), None)
    if not last_user_msg:
        raise HTTPException(status_code=400, detail="No user message found")

    system_prompt = request.system_prompt or "You are a helpful AI assistant."

    # ── Daily limit check ─────────────────────────────────────────────────────
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    used_today = await db.scalar(
        select(func.count(Message.id)).where(
            Message.user_id == current_user.id,
            Message.role == "user",
            Message.created_at >= today_start,
        )
    ) or 0
    if current_user.daily_message_limit and used_today >= current_user.daily_message_limit:
        raise HTTPException(
            status_code=429,
            detail={"used": used_today, "limit": current_user.daily_message_limit},
        )

    # ── Ensure session exists ─────────────────────────────────────────────────
    session_id = str(request.session_id) if request.session_id else str(uuid.uuid4())
    session = await db.scalar(select(ChatSession).where(ChatSession.id == session_id))
    if session and session.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session:
        title = last_user_msg[:60] + ("…" if len(last_user_msg) > 60 else "")
        session = ChatSession(id=session_id, user_id=current_user.id, title=title)
        db.add(session)
        await db.commit()

    # ── Per-user PS client / mode ─────────────────────────────────────────────
    ps_mode        = current_user.ps_mode or "api"
    ps_client: Optional[PromptSecurityClient] = None
    ps_gw_client: Optional[AsyncOpenAI] = None    # gateway-mode LLM client (OpenAI-compat providers)
    ps_gw_gemini: Optional[dict]        = None    # gateway-mode config for Gemini (native path)
    ps_gw_anthropic: Optional[dict]     = None    # gateway-mode config for Anthropic (native path)

    if current_user.ps_enabled and current_user.ps_tenant and current_user.ps_api_key_enc:
        try:
            ps_app_id = decrypt(current_user.ps_api_key_enc)
        except ValueError:
            logger.warning("PS key decrypt failed for %s — key rotated? Ask user to re-enter PS key.", current_user.email)
            ps_app_id = None
        if ps_app_id:
            try:
                if ps_mode == "api":
                    ps_client = PromptSecurityClient(
                        base_url=current_user.ps_tenant.base_url,
                        app_id=ps_app_id,
                    )
                elif ps_mode == "gateway" and current_user.ps_tenant.gateway_url:
                    llm_key = _get_llm_key(current_user, model)
                    gw_host = current_user.ps_tenant.gateway_url.rstrip('/')
                    gw_base = gw_host if gw_host.endswith('/v1') else gw_host + '/v1'
                    logger.info("PS gateway init for %s → %s model=%s (llm_key set: %s)",
                                current_user.email, gw_base, model, bool(llm_key))
                    ps_root = gw_host[:-3] if gw_host.endswith('/v1') else gw_host
                    if llm_key:
                        if model.startswith('gemini-'):
                            gemini_url = f"{ps_root}/v1beta/models/{model}:generateContent"
                            logger.info("PS Gemini gateway URL → %s", gemini_url)
                            ps_gw_gemini = {'url': gemini_url, 'llm_key': llm_key}
                        elif model.startswith('claude-'):
                            anthropic_url = f"{ps_root}/v1/messages"
                            logger.info("PS Anthropic gateway URL → %s model=%s", anthropic_url, model)
                            ps_gw_anthropic = {'url': anthropic_url, 'llm_key': llm_key, 'model': model}
                        else:
                            # PS routes OpenAI/Perplexity via LLM API key alone (OpenAI-compat)
                            logger.info("PS gateway base_url → %s model=%s", gw_base, model)
                            ps_gw_client = AsyncOpenAI(
                                api_key=llm_key,
                                base_url=gw_base,
                                timeout=30.0,
                                default_headers={"ps-user": current_user.email},
                            )
                    else:
                        logger.warning("Gateway mode: no LLM key for %s — PS gateway requires the provider API key", current_user.email)
            except Exception as e:
                logger.warning("Could not init PS client for %s: %s", current_user.email, e)

    if request.skip_ps and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="skip_ps is restricted to admin users")

    # ── Store user message in DB ──────────────────────────────────────────────
    user_db_msg = Message(
        session_id=session_id,
        user_id=current_user.id,
        role="user",
        content=last_user_msg,
        model=model,
    )
    db.add(user_db_msg)
    await db.commit()

    skip_ps = request.skip_ps

    async def generate():
        reply = ""
        prompt_action = "pass"
        t0 = time.monotonic()
        prompt_tokens: Optional[int] = None
        completion_tokens: Optional[int] = None
        total_tokens: Optional[int] = None
        ps_prompt_raw: Optional[dict] = None
        ps_resp_raw: Optional[dict] = None

        msgs = list(request.messages)
        payload = [{"role": "system", "content": system_prompt}] + [
            {"role": m.role, "content": _build_content(m)} for m in msgs
        ]

        # ── Gateway mode: route through PS proxy, skip explicit scanning ───────
        if ps_gw_gemini and not skip_ps:
            system_parts, contents = [], []
            for msg in payload:
                role = msg.get('role', 'user')
                content = msg.get('content', '')
                if isinstance(content, list):
                    content = ' '.join(p.get('text', '') for p in content if isinstance(p, dict))
                if role == 'system':
                    system_parts.append({"text": content})
                else:
                    contents.append({"role": "user" if role == "user" else "model",
                                     "parts": [{"text": content}]})
            gemini_body: dict = {"contents": contents}
            if system_parts:
                gemini_body["system_instruction"] = {"parts": system_parts}
            try:
                async with httpx.AsyncClient(timeout=10.0) as hclient:
                    resp = await hclient.post(ps_gw_gemini['url'],
                        headers={
                            "Content-Type": "application/json",
                            "x-goog-api-key": ps_gw_gemini['llm_key'],
                            "ps-user": current_user.email,
                        },
                        json=gemini_body,
                    )
                    if resp.status_code != 200:
                        raise Exception(f"Gemini gateway {resp.status_code}: {resp.text[:300]}")
                    data = resp.json()
                    text = data['candidates'][0]['content']['parts'][0].get('text', '')
                    if text:
                        reply = text
                        yield f"data: {json.dumps({'type': 'token', 'content': text})}\n\n"
            except Exception as e:
                detail = f"Gateway error: {e}"
                logger.error(detail)
                yield f"data: {json.dumps({'type': 'error', 'detail': detail})}\n\n"
                return
            resp_ms = round((time.monotonic() - t0) * 1000)
            await _log_msg(db, session_id, current_user.id, "assistant", reply, model,
                           ps_scanned=True, ps_action="pass", response_ms=resp_ms)
            today_used = used_today + 1
            yield f"data: {json.dumps({'type': 'done', 'model': model, 'session_id': session_id, 'ps_scanned': True, 'ps_action': 'gateway', 'ps_violations': [], 'messages_today': today_used, 'daily_limit': current_user.daily_message_limit, 'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0})}\n\n"
            return

        if ps_gw_anthropic and not skip_ps:
            anthropic_messages = []
            system_text = ""
            for msg in payload:
                role = msg.get('role', 'user')
                content = msg.get('content', '')
                if isinstance(content, list):
                    content = ' '.join(p.get('text', '') for p in content if isinstance(p, dict))
                if role == 'system':
                    system_text = content
                else:
                    anthropic_messages.append({"role": role, "content": content})
            anthropic_body: dict = {
                "model": ps_gw_anthropic['model'],
                "messages": anthropic_messages,
                "max_tokens": 1024,
                "stream": True,
            }
            if system_text:
                anthropic_body["system"] = system_text
            try:
                async with httpx.AsyncClient(timeout=30.0) as hclient:
                    async with hclient.stream('POST', ps_gw_anthropic['url'],
                        headers={
                            "Content-Type": "application/json",
                            "x-api-key": ps_gw_anthropic['llm_key'],
                            "anthropic-version": "2023-06-01",
                            "ps-user": current_user.email,
                        },
                        json=anthropic_body,
                    ) as resp:
                        if resp.status_code != 200:
                            body_bytes = await resp.aread()
                            raise Exception(f"Anthropic gateway {resp.status_code}: {body_bytes[:300]}")
                        async for line in resp.aiter_lines():
                            if not line.startswith('data: '):
                                continue
                            data_str = line[6:]
                            try:
                                event = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue
                            etype = event.get('type')
                            if etype == 'content_block_delta':
                                delta = event.get('delta', {})
                                if delta.get('type') == 'text_delta':
                                    text = delta.get('text', '')
                                    if text:
                                        reply += text
                                        yield f"data: {json.dumps({'type': 'token', 'content': text})}\n\n"
                            elif etype == 'message_start':
                                u = event.get('message', {}).get('usage', {})
                                prompt_tokens = u.get('input_tokens', 0)
                            elif etype == 'message_delta':
                                u = event.get('usage', {})
                                completion_tokens = u.get('output_tokens', 0)
            except Exception as e:
                detail = f"Anthropic gateway error: {e}"
                logger.error(detail)
                yield f"data: {json.dumps({'type': 'error', 'detail': detail})}\n\n"
                return
            resp_ms = round((time.monotonic() - t0) * 1000)
            prompt_tokens = prompt_tokens or estimate_message_tokens(payload, model=model)
            completion_tokens = completion_tokens or estimate_text_tokens(reply, model=model)
            total_tokens = (prompt_tokens or 0) + (completion_tokens or 0)
            await _log_msg(db, session_id, current_user.id, "assistant", reply, model,
                           ps_scanned=True, ps_action="pass", response_ms=resp_ms)
            today_used = used_today + 1
            yield f"data: {json.dumps({'type': 'done', 'model': model, 'session_id': session_id, 'ps_scanned': True, 'ps_action': 'gateway', 'ps_violations': [], 'messages_today': today_used, 'daily_limit': current_user.daily_message_limit, 'prompt_tokens': prompt_tokens, 'completion_tokens': completion_tokens, 'total_tokens': total_tokens})}\n\n"
            return

        if ps_gw_client and not skip_ps:
            try:
                prompt_tokens = estimate_message_tokens(payload, model=model)
                stream = await ps_gw_client.chat.completions.create(
                    model=model, messages=payload, stream=True
                )
                async for chunk in stream:
                    if getattr(chunk, "usage", None):
                        prompt_tokens = getattr(chunk.usage, "prompt_tokens", prompt_tokens)
                        completion_tokens = getattr(chunk.usage, "completion_tokens", completion_tokens)
                        total_tokens = getattr(chunk.usage, "total_tokens", total_tokens)
                    if chunk.choices and chunk.choices[0].delta.content:
                        token = chunk.choices[0].delta.content
                        reply += token
                        yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
            except openai.BadRequestError as e:
                body = str(e).lower()
                logger.error("PS gateway 400 error: %s", e)
                # Only treat as a PS policy block if the body explicitly says so
                if "block" in body or "policy" in body or "violat" in body or "denied" in body:
                    await _log_msg(db, session_id, current_user.id, "assistant", "", model,
                                   ps_scanned=True, ps_blocked=True, ps_action="block")
                    yield f"data: {json.dumps({'type': 'blocked', 'action': 'block', 'violations': []})}\n\n"
                else:
                    detail = f"Gateway config error (400): {e}"
                    logger.error(detail)
                    yield f"data: {json.dumps({'type': 'error', 'detail': detail})}\n\n"
                return
            except openai.AuthenticationError as e:
                detail = f"Gateway auth failed — check PS App ID: {e}"
                logger.error(detail)
                yield f"data: {json.dumps({'type': 'error', 'detail': detail})}\n\n"
                return
            except openai.PermissionDeniedError as e:
                body = str(e).lower()
                logger.error("PS gateway 403: %s", e)
                if "block" in body or "policy" in body or "violat" in body:
                    await _log_msg(db, session_id, current_user.id, "assistant", "", model,
                                   ps_scanned=True, ps_blocked=True, ps_action="block")
                    yield f"data: {json.dumps({'type': 'blocked', 'action': 'block', 'violations': []})}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'error', 'detail': f'Gateway permission denied: {e}'})}\n\n"
                return
            except Exception as e:
                detail = f"Gateway error: {e}"
                logger.error(detail)
                yield f"data: {json.dumps({'type': 'error', 'detail': detail})}\n\n"
                return

            resp_ms = round((time.monotonic() - t0) * 1000)
            completion_tokens = completion_tokens or estimate_text_tokens(reply, model=model)
            prompt_tokens = prompt_tokens or estimate_message_tokens(payload, model=model)
            total_tokens = total_tokens or (prompt_tokens + completion_tokens)
            await _log_msg(db, session_id, current_user.id, "assistant", reply, model,
                           ps_scanned=True, ps_action="pass", response_ms=resp_ms)
            today_used = used_today + 1
            yield f"data: {json.dumps({'type': 'done', 'model': model, 'session_id': session_id, 'ps_scanned': True, 'ps_action': 'gateway', 'ps_violations': [], 'messages_today': today_used, 'daily_limit': current_user.daily_message_limit, 'prompt_tokens': prompt_tokens, 'completion_tokens': completion_tokens, 'total_tokens': total_tokens})}\n\n"
            return

        # ── API mode: explicit PS scan + LiteLLM/per-user key ─────────────────
        prompt_violations: list = []
        if ps_client and not skip_ps:
            try:
                prompt_tok_est = estimate_text_tokens(last_user_msg)
                logger.info("PS prompt scan: user=%s, prompt_chars=%d, estimated_tokens=%d, prompt_preview=%.120s",
                            current_user.email, len(last_user_msg), prompt_tok_est, last_user_msg)
                ps_result = await ps_client.protect_prompt(
                    user_prompt=last_user_msg,
                    system_prompt=system_prompt,
                    user=current_user.email,
                )
                logger.info("PS prompt result: action=%s, allowed=%s, violations=%s, modified=%s",
                            ps_result.action, ps_result.allowed, ps_result.violations, bool(ps_result.modified_text))
                prompt_violations = ps_result.violations
                ps_prompt_raw = {"request": ps_result.raw_request, "response": ps_result.raw}
                if not ps_result.allowed:
                    await _log_msg(db, session_id, current_user.id, "assistant", "", model,
                                   ps_scanned=True, ps_blocked=True, ps_action="block",
                                   ps_violations=ps_result.violations)
                    yield f"data: {json.dumps({'type': 'blocked', 'action': 'block', 'violations': ps_result.violations, 'ps_raw': {'prompt': ps_prompt_raw}})}\n\n"
                    return
                if ps_result.modified_text:
                    last_user_msg_eff = ps_result.modified_text
                    prompt_action = "modify"
                    for i in range(len(payload) - 1, -1, -1):
                        if payload[i]["role"] == "user":
                            payload[i]["content"] = last_user_msg_eff
                            break
                else:
                    last_user_msg_eff = last_user_msg
            except Exception as e:
                logger.error("PS prompt scan error for %s: %s", current_user.email, e)
                err_hint = "PS App ID may be wrong for this tenant — re-enter it in ⚙ Settings → Prompt Security."
                yield ("data: " + json.dumps({'type': 'error', 'detail': f'PS scan failed: {e}. {err_hint}'}) + "\n\n")
                return
        else:
            last_user_msg_eff = last_user_msg

        # ── Stream (per-user key or shared LiteLLM) ────────────────────────────
        llm, effective_model = _user_llm_client(current_user, model)
        try:
            prompt_tokens = estimate_message_tokens(payload, model=effective_model)
            stream_kwargs = {"model": effective_model, "messages": payload, "stream": True}
            if llm is litellm_client:
                stream_kwargs["stream_options"] = {"include_usage": True}
                stream_kwargs["extra_body"] = _litellm_extra(effective_model)
            stream = await llm.chat.completions.create(**stream_kwargs)
            async for chunk in stream:
                if getattr(chunk, "usage", None):
                    prompt_tokens = getattr(chunk.usage, "prompt_tokens", prompt_tokens)
                    completion_tokens = getattr(chunk.usage, "completion_tokens", completion_tokens)
                    total_tokens = getattr(chunk.usage, "total_tokens", total_tokens)
                if chunk.choices and chunk.choices[0].delta.content:
                    token = chunk.choices[0].delta.content
                    reply += token
                    yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
        except Exception as e:
            logger.error("LLM stream error: %s", e)
            yield f"data: {json.dumps({'type': 'error', 'detail': str(e)})}\n\n"
            return

        resp_ms = round((time.monotonic() - t0) * 1000)

        # ── PS: scan response ─────────────────────────────────────────────────
        final_action = prompt_action
        ps_violations: list = list(prompt_violations)
        if ps_client and not skip_ps and reply:
            try:
                ps_resp = await ps_client.protect_response(
                    response_text=reply,
                    user=current_user.email,
                )
                ps_violations = prompt_violations + ps_resp.violations
                ps_resp_raw = {"request": ps_resp.raw_request, "response": ps_resp.raw}
                if not ps_resp.allowed:
                    await _log_msg(db, session_id, current_user.id, "assistant", reply, model,
                                   ps_scanned=True, ps_blocked=True, ps_action="block",
                                   ps_violations=ps_violations, response_ms=resp_ms)
                    yield f"data: {json.dumps({'type': 'revoke', 'action': 'block', 'violations': ps_violations, 'ps_raw': {'prompt': ps_prompt_raw, 'response': ps_resp_raw}})}\n\n"
                    return
                if ps_resp.modified_text:
                    reply = ps_resp.modified_text
                    final_action = "modify"
                    yield f"data: {json.dumps({'type': 'sanitized', 'text': reply})}\n\n"
            except Exception as e:
                logger.error("PS response scan error for %s: %s", current_user.email, e)
                err_hint = "PS App ID may be wrong for this tenant — re-enter it in ⚙ Settings → Prompt Security."
                yield ("data: " + json.dumps({'type': 'error', 'detail': f'PS response scan failed: {e}. {err_hint}'}) + "\n\n")
                return

        completion_tokens = completion_tokens or estimate_text_tokens(reply, model=effective_model)
        prompt_tokens = prompt_tokens or estimate_message_tokens(payload, model=effective_model)
        total_tokens = total_tokens or (prompt_tokens + completion_tokens)

        ps_active = bool(ps_client) and not skip_ps
        try:
            await _log_msg(db, session_id, current_user.id, "assistant", reply, model,
                           ps_scanned=ps_active, ps_action=final_action,
                           ps_violations=ps_violations, response_ms=resp_ms)
        except Exception as e:
            logger.error("Failed to log assistant message: %s", e)

        today_used = used_today + 1
        ps_raw_payload = {"prompt": ps_prompt_raw, "response": ps_resp_raw} if ps_active else None
        yield f"data: {json.dumps({'type': 'done', 'model': model, 'session_id': session_id, 'ps_scanned': ps_active, 'ps_action': final_action, 'ps_violations': ps_violations, 'messages_today': today_used, 'daily_limit': current_user.daily_message_limit, 'prompt_tokens': prompt_tokens, 'completion_tokens': completion_tokens, 'total_tokens': total_tokens, 'ps_raw': ps_raw_payload})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── File Sanitization ─────────────────────────────────────────────────────────
@app.post("/upload/sanitize")
async def upload_sanitize(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    if not (current_user.ps_enabled and current_user.ps_tenant and current_user.ps_api_key_enc):
        raise HTTPException(status_code=400, detail="Prompt Security is not configured. Set up PS in Settings first.")
    try:
        ps_app_id = decrypt(current_user.ps_api_key_enc)
    except ValueError:
        raise HTTPException(status_code=400, detail="PS key could not be decrypted — re-enter it in Settings.")
    ps_client = PromptSecurityClient(base_url=current_user.ps_tenant.base_url, app_id=ps_app_id)
    mime = (file.content_type or "application/octet-stream").split(";")[0].strip()
    filename = file.filename or "upload"
    if mime not in ALLOWED_SANITIZE_TYPES and not filename.lower().endswith(ALLOWED_SANITIZE_EXTENSIONS):
        raise HTTPException(status_code=415, detail=f"Unsupported file type '{mime}'.")

    await _acquire_sanitize_slot(current_user.id)
    try:
        file_bytes = await _read_upload_with_limit(file)

        # Record per-minute quota only after local validation/read succeeds.
        now = time.time()
        async with _sanitize_guard_lock:
            timestamps = _sanitize_user_timestamps[current_user.id]
            while timestamps and now - timestamps[0] > 60:
                timestamps.popleft()
            if len(timestamps) >= SANITIZE_MAX_PER_MINUTE:
                raise HTTPException(status_code=429, detail="Sanitize rate limit exceeded")
            timestamps.append(now)

        t0 = time.monotonic()
        try:
            job_id = await ps_client.sanitize_file_submit(file_bytes, file.filename or "upload")
            result = await ps_client.sanitize_file_poll(job_id, max_seconds=30)
        except TimeoutError as e:
            raise HTTPException(status_code=504, detail=str(e))
        except Exception as e:
            logger.error("File sanitization error for %s: %s", current_user.email, e)
            raise HTTPException(status_code=502, detail=f"PS file sanitization failed: {e}")
        scan_ms = round((time.monotonic() - t0) * 1000)
        action = result.get("action", result.get("status", "pass"))
        violations = result.get("violations", [])
        sanitized_url = result.get("sanitizedFileUrl") or result.get("sanitized_file_url") or result.get("url")
        return {
            "job_id": job_id,
            "action": action,
            "violations": violations,
            "sanitized_url": sanitized_url,
            "scan_ms": scan_ms,
            "raw": result,
        }
    finally:
        await _release_sanitize_slot(current_user.id)


# ── User: LLM key overrides ───────────────────────────────────────────────────
@app.patch("/users/me/llm-keys", response_model=UserOut)
async def update_my_llm_keys(
    body: LLMKeysUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    existing: dict = {}
    if current_user.llm_api_keys_enc:
        try:
            existing = json.loads(decrypt(current_user.llm_api_keys_enc))
        except Exception:
            pass

    for provider in ("openai", "anthropic", "google", "openrouter"):
        val = getattr(body, provider)
        if val is None:
            continue
        if val.strip():
            existing[provider] = val.strip()
        else:
            existing.pop(provider, None)

    current_user.llm_api_keys_enc = encrypt(json.dumps(existing)) if existing else None
    await db.commit()
    await db.refresh(current_user, ["ps_tenant"])
    updated = [p for p in ("openai","anthropic","google","openrouter") if getattr(body, p) is not None]
    await _log_audit(db, current_user.id, current_user.email, "llm_keys_updated", "providers: " + ", ".join(updated))
    return _user_out(current_user)


# ── User: Validate LLM API key ──────────────────────────────────────────────
@app.post("/users/me/validate-llm-key")
async def validate_llm_key(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    body = await request.json()
    provider = body.get("provider", "")
    api_key = body.get("api_key", "")
    if not provider or not api_key:
        raise HTTPException(status_code=400, detail="provider and api_key are required")

    urls = {
        "openai": "https://api.openai.com/v1/models",
        "google": "https://generativelanguage.googleapis.com/v1beta/models",
        "perplexity": "https://api.perplexity.ai/models",
        "openrouter": "https://openrouter.ai/api/v1/models",
    }

    if provider == "anthropic":
        # Anthropic has no /models endpoint; verify with a small messages call
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
                    json={"model": "claude-sonnet-4-5-20250929", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]},
                )
                if resp.status_code in (200, 201):
                    return {"valid": True, "provider": provider}
                elif resp.status_code == 401:
                    return {"valid": False, "provider": provider, "error": "Invalid API key"}
                else:
                    return {"valid": True, "provider": provider}  # non-401 likely means key is valid but other issue
        except Exception as e:
            return {"valid": False, "provider": provider, "error": str(e)}

    url = urls.get(provider)
    if not url:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            headers = {"Authorization": f"Bearer {api_key}"}
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                model_count = len(data.get("data", data.get("models", [])))
                return {"valid": True, "provider": provider, "models_count": model_count}
            elif resp.status_code == 401:
                return {"valid": False, "provider": provider, "error": "Invalid API key"}
            else:
                return {"valid": False, "provider": provider, "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"valid": False, "provider": provider, "error": str(e)}


# ── Admin: Activity log ───────────────────────────────────────────────────────
@app.get("/admin/activity")
async def admin_activity(
    limit: int = 100,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    msg_result = await db.execute(
        select(Message, User.email)
        .outerjoin(User, User.id == Message.user_id)
        .order_by(desc(Message.created_at))
        .limit(min(limit, 500))
    )
    chat_rows = [
        {
            "id": f"msg-{msg.id}",
            "entry_type": "chat",
            "user_email": email or msg.guest_id or "guest",
            "session_id": msg.session_id,
            "role": msg.role,
            "content_preview": msg.content[:120] + ("…" if len(msg.content) > 120 else ""),
            "model": msg.model,
            "ps_scanned": msg.ps_scanned,
            "ps_action": msg.ps_action,
            "ps_violations": msg.ps_violations,
            "response_ms": msg.response_ms,
            "created_at": msg.created_at.isoformat(),
        }
        for msg, email in msg_result.all()
    ]

    audit_result = await db.execute(
        select(AuditEvent)
        .order_by(desc(AuditEvent.created_at))
        .limit(min(limit, 200))
    )
    audit_rows = [
        {
            "id": f"audit-{ev.id}",
            "entry_type": "audit",
            "user_email": ev.user_email,
            "role": "system",
            "event_type": ev.event_type,
            "content_preview": ev.detail or ev.event_type,
            "detail": ev.detail,
            "model": None,
            "ps_scanned": False,
            "ps_action": None,
            "created_at": ev.created_at.isoformat(),
        }
        for ev in audit_result.scalars().all()
    ]

    combined = sorted(chat_rows + audit_rows, key=lambda r: r["created_at"], reverse=True)
    return combined[:min(limit, 500)]


@app.delete("/admin/activity", status_code=204)
async def clear_activity(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(text("DELETE FROM audit_events"))
    await db.execute(text("DELETE FROM messages"))
    await db.execute(text("DELETE FROM chat_sessions"))
    await db.commit()


@app.post("/admin/reset-database", status_code=204)
async def reset_database(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Wipe all data and settings, then re-seed the default admin user and demo scenarios."""
    for table in [
        "audit_events", "messages", "chat_sessions",
        "api_keys", "users", "ps_tenants", "demo_scenarios", "app_settings",
    ]:
        await db.execute(text(f"DELETE FROM {table}"))
    await db.commit()

    # Re-create a bootstrap admin placeholder so the wizard can be completed again
    import secrets as _secrets
    new_admin = User(
        email=f"setup-{_secrets.token_hex(8)}@wizard.internal",
        hashed_password=hash_password(_secrets.token_hex(32)),
        role="admin",
        is_active=True,
        must_change_password=True,
    )
    db.add(new_admin)
    await db.commit()

    await _seed_demo_scenarios(db)

    # Clear the on-disk encryption key override file so the wizard starts clean
    clear_encryption_key_override()
    # Reset in-memory JWT to ephemeral so setup wizard can re-issue a bootstrap token
    reset_secret_key()


# ── App settings ─────────────────────────────────────────────────────────────

def _parse_app_settings(s: dict) -> dict:
    try:
        domains = json.loads(s.get("allowed_email_domains", "[]"))
        if not isinstance(domains, list):
            domains = []
    except Exception:
        domains = []
    try:
        smtp_port_val = int(s.get("smtp_port", "587") or "587")
    except (ValueError, TypeError):
        smtp_port_val = 587
    return {
        "user_mgmt_enabled": s.get("user_mgmt_enabled", "true") != "false",
        "fixed_model_enabled": s.get("fixed_model_enabled", "false") == "true",
        "fixed_model_id": s.get("fixed_model_id", None),
        "allowed_email_domains": domains,
        # Email settings
        "smtp_host": s.get("smtp_host", ""),
        "smtp_port": smtp_port_val,
        "email_username": s.get("email_username", ""),
        "email_password_set": bool(s.get("email_password_enc")),
        "from_email": s.get("from_email", ""),
        "jwt_secret_set": bool(s.get("jwt_secret_enc")),
        "admin_password_set": bool(s.get("admin_password_hash")),
        "litellm_key_set": bool(s.get("litellm_key_enc")),
        # Application settings
        "daily_limit": int(s["daily_limit"]) if s.get("daily_limit") else DEFAULT_DAILY_LIMIT,
        "max_file_mb": int(s["max_file_mb"]) if s.get("max_file_mb") else MAX_FILE_SIZE_MB,
        "ollama_enabled": s.get("ollama_enabled", "false") == "true",
        "ollama_base_url": s.get("ollama_base_url", OLLAMA_BASE_URL),
        "ollama_model_ids": s.get("ollama_model_ids", ",".join(sorted(_OLLAMA_MODEL_IDS))),
        "wizard_completed": s.get("wizard_completed") == "true",
    }


@app.get("/app/settings")
async def get_app_settings(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(AppSetting))
    s = {row.key: row.value for row in result.scalars().all()}
    parsed = _parse_app_settings(s)
    # True if SMTP will actually work — DB value takes precedence, env var is fallback
    parsed["smtp_configured"] = bool(parsed.get("smtp_host") or SMTP_HOST)
    parsed["encryption_key_set"] = encryption_key_overridden()
    return parsed


@app.patch("/admin/app-settings")
async def update_app_settings(
    body: dict,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    # Keys that should be stored as-is (no lowercasing)
    _plain_str_keys = {"smtp_host", "smtp_port", "email_username", "from_email"}
    for key, value in body.items():
        if key == "allowed_email_domains":
            # Store as a JSON array; validate it's a list of strings
            if not isinstance(value, list):
                raise HTTPException(status_code=422, detail="allowed_email_domains must be a list")
            serialised = json.dumps([str(d).lower().strip() for d in value if str(d).strip()])
            store_key = key
        elif key == "email_password":
            # Encrypt password and store under a different key; never store plaintext
            serialised = encrypt(str(value))
            store_key = "email_password_enc"
        elif key in _plain_str_keys:
            serialised = str(value)
            store_key = key
        else:
            serialised = str(value).lower()
            store_key = key
        existing = await db.get(AppSetting, store_key)
        if existing:
            existing.value = serialised
        else:
            db.add(AppSetting(key=store_key, value=serialised))
    await db.commit()
    result = await db.execute(select(AppSetting))
    s = {row.key: row.value for row in result.scalars().all()}
    return _parse_app_settings(s)


class JwtSecretUpdate(BaseModel):
    secret: str


@app.post("/admin/jwt-secret")
async def update_jwt_secret(
    body: JwtSecretUpdate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Store a new JWT signing secret, hot-swap it in-process, and persist to DB. Invalidates all active sessions."""
    if not body.secret or len(body.secret) < 32:
        raise HTTPException(status_code=422, detail="Secret must be at least 32 characters")

    enc = encrypt(body.secret)
    existing = await db.get(AppSetting, "jwt_secret_enc")
    if existing:
        existing.value = enc
    else:
        db.add(AppSetting(key="jwt_secret_enc", value=enc))
    await db.commit()

    await _log_audit(db, admin.id, admin.email, "jwt_secret_changed", "JWT signing secret updated via admin UI")
    await db.commit()

    set_secret_key(body.secret)
    return {"ok": True}


class EncryptionKeyUpdate(BaseModel):
    key: str


@app.post("/admin/encryption-key")
async def update_encryption_key(
    body: EncryptionKeyUpdate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Persist a new Fernet encryption key and hot-swap it in-process.
    WARNING: any data encrypted with the old key (PS API keys, email password, etc.) will no longer be readable."""
    if not body.key:
        raise HTTPException(status_code=422, detail="Key cannot be empty")
    if not validate_fernet_key(body.key):
        raise HTTPException(status_code=422, detail="Invalid Fernet key — must be a URL-safe base64-encoded 32-byte value")

    write_encryption_key_override(body.key)
    set_encryption_key(body.key)

    await _log_audit(db, admin.id, admin.email, "encryption_key_changed", "Fernet encryption key updated via admin UI")
    await db.commit()

    return {"ok": True}


class AdminPasswordUpdate(BaseModel):
    password: str
    confirm: str
    email: Optional[str] = None


@app.post("/admin/admin-password")
async def update_admin_password(
    body: AdminPasswordUpdate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Set the admin password. Optionally update the admin email (used by the setup wizard)."""
    if not body.password:
        raise HTTPException(status_code=422, detail="Password cannot be empty")
    if body.password != body.confirm:
        raise HTTPException(status_code=422, detail="Passwords do not match")

    if body.email:
        new_email = body.email.strip().lower()
        if not new_email or "@" not in new_email:
            raise HTTPException(status_code=422, detail="Invalid email address")
        conflict = await db.scalar(
            select(User).where(User.email == new_email, User.id != admin.id)
        )
        if conflict:
            raise HTTPException(status_code=409, detail="Email already in use")
        admin.email = new_email
        admin.must_change_password = False

    hashed = hash_password(body.password)
    existing = await db.get(AppSetting, "admin_password_hash")
    if existing:
        existing.value = hashed
    else:
        db.add(AppSetting(key="admin_password_hash", value=hashed))
    await db.commit()

    await _log_audit(db, admin.id, admin.email, "admin_password_changed", "Admin password updated")
    await db.commit()
    return {"ok": True}


class LiteLLMKeyUpdate(BaseModel):
    key: str


@app.post("/admin/litellm-key")
async def update_litellm_key(
    body: LiteLLMKeyUpdate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Store and hot-swap the LiteLLM master key. Note: the LiteLLM container must also be restarted with the new key."""
    if not body.key:
        raise HTTPException(status_code=422, detail="Key cannot be empty")

    enc = encrypt(body.key)
    existing = await db.get(AppSetting, "litellm_key_enc")
    if existing:
        existing.value = enc
    else:
        db.add(AppSetting(key="litellm_key_enc", value=enc))
    await db.commit()

    await _log_audit(db, admin.id, admin.email, "litellm_key_changed", "LiteLLM master key updated via admin UI")
    await db.commit()

    set_litellm_master_key(body.key)
    return {"ok": True}


@app.delete("/admin/jwt-secret")
async def clear_jwt_secret(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Remove the stored JWT secret and revert to the env-var value (ephemeral in dev)."""
    row = await db.get(AppSetting, "jwt_secret_enc")
    if row:
        await db.delete(row)
        await db.commit()
    reset_secret_key()
    await _log_audit(db, admin.id, admin.email, "jwt_secret_cleared", "JWT secret cleared via admin UI — reverted to env default")
    await db.commit()
    return {"ok": True}


@app.delete("/admin/encryption-key")
async def clear_encryption_key(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Remove the stored encryption key override and revert to the env-var value (or new ephemeral key)."""
    clear_encryption_key_override()
    await _log_audit(db, admin.id, admin.email, "encryption_key_cleared", "Encryption key override cleared via admin UI — reverted to env default")
    await db.commit()
    return {"ok": True}


@app.delete("/admin/litellm-key")
async def clear_litellm_key(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Remove the stored LiteLLM master key."""
    row = await db.get(AppSetting, "litellm_key_enc")
    if row:
        await db.delete(row)
        await db.commit()
    set_litellm_master_key("")
    await _log_audit(db, admin.id, admin.email, "litellm_key_cleared", "LiteLLM master key cleared via admin UI")
    await db.commit()
    return {"ok": True}


class ApplicationSettingsUpdate(BaseModel):
    daily_limit: int | None = None
    max_file_mb: int | None = None
    ollama_enabled: bool | None = None
    ollama_base_url: str | None = None
    ollama_model_ids: str | None = None


@app.patch("/admin/application-settings")
async def update_application_settings(
    body: ApplicationSettingsUpdate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    global DEFAULT_DAILY_LIMIT, MAX_FILE_SIZE_MB, MAX_FILE_SIZE_BYTES, _OLLAMA_MODEL_IDS, OLLAMA_BASE_URL

    async def _upsert(key: str, value: str) -> None:
        existing = await db.get(AppSetting, key)
        if existing:
            existing.value = value
        else:
            db.add(AppSetting(key=key, value=value))

    if body.daily_limit is not None:
        if body.daily_limit < 0:
            raise HTTPException(status_code=422, detail="daily_limit must be 0 or greater (0 = unlimited)")
        await _upsert("daily_limit", str(body.daily_limit))
        DEFAULT_DAILY_LIMIT = body.daily_limit or None

    if body.max_file_mb is not None:
        if body.max_file_mb < 1:
            raise HTTPException(status_code=422, detail="max_file_mb must be at least 1")
        await _upsert("max_file_mb", str(body.max_file_mb))
        MAX_FILE_SIZE_MB = body.max_file_mb
        MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

    if body.ollama_enabled is not None:
        await _upsert("ollama_enabled", "true" if body.ollama_enabled else "false")

    if body.ollama_base_url is not None:
        url = body.ollama_base_url.strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            raise HTTPException(status_code=422, detail="ollama_base_url must start with http:// or https://")
        await _upsert("ollama_base_url", url)
        OLLAMA_BASE_URL = url

    litellm_restarting = False
    if body.ollama_model_ids is not None:
        ids = body.ollama_model_ids.strip()
        await _upsert("ollama_model_ids", ids)
        _OLLAMA_MODEL_IDS = {s.strip() for s in ids.split(",") if s.strip()}
        model_id_list = [s.strip() for s in ids.split(",") if s.strip()]
        if model_id_list:
            try:
                _update_litellm_ollama_models(model_id_list)
                asyncio.create_task(_restart_litellm_and_refresh())
                litellm_restarting = True
            except Exception as exc:
                logger.warning("Could not update LiteLLM config: %s", exc)

    await db.commit()
    await _log_audit(db, admin.id, admin.email, "application_settings_changed", "Application settings updated via admin UI")
    await db.commit()
    return {"ok": True, "litellm_restarting": litellm_restarting}


# ── LiteLLM config + restart helpers ─────────────────────────────────────────

LITELLM_CONFIG_PATH = Path(os.getenv("LITELLM_CONFIG_PATH", "/app/litellm/config.yaml"))


def _update_litellm_ollama_models(model_ids: list[str]) -> None:
    """Rewrite the ollama/ entries in litellm/config.yaml, preserving all other models."""
    import yaml  # pyyaml — available at runtime inside the container

    if not LITELLM_CONFIG_PATH.exists():
        logger.warning("LiteLLM config not found at %s — skipping YAML update", LITELLM_CONFIG_PATH)
        return

    cfg = yaml.safe_load(LITELLM_CONFIG_PATH.read_text()) or {}
    model_list = cfg.get("model_list", [])

    # Drop all existing ollama/* entries (we'll re-add the full set)
    model_list = [
        m for m in model_list
        if not str(m.get("litellm_params", {}).get("model", "")).startswith("ollama/")
    ]

    # Append fresh entries for every selected model
    for mid in model_ids:
        model_list.append({
            "model_name": mid,
            "litellm_params": {
                "model": f"ollama/{mid}",
                "api_base": "os.environ/OLLAMA_BASE_URL",
            },
        })

    cfg["model_list"] = model_list
    LITELLM_CONFIG_PATH.write_text(
        yaml.dump(cfg, default_flow_style=False, allow_unicode=True, sort_keys=False)
    )
    logger.info("Updated litellm/config.yaml with Ollama models: %s", model_ids)


async def _restart_litellm_and_refresh() -> None:
    """Restart the LiteLLM container so it picks up the updated config, then rebuild the model cache."""
    if not _DOCKER_SDK_AVAILABLE:
        logger.warning("Docker SDK unavailable — cannot restart LiteLLM")
        return
    try:
        client = _docker_sdk.from_env()
        containers = client.containers.list(filters={
            "label": [
                "com.docker.compose.service=litellm",
                f"com.docker.compose.project={COMPOSE_PROJECT_NAME}",
            ]
        })
        if not containers:
            logger.warning("LiteLLM container not found — skipping restart")
            return
        containers[0].restart()
        logger.info("LiteLLM container restarted to pick up new Ollama model config")
        await asyncio.sleep(8)          # wait for LiteLLM to come back up
        await refresh_model_cache()
        logger.info("Model cache refreshed after LiteLLM restart")
    except Exception as exc:
        logger.warning("Failed to restart LiteLLM container: %s", exc)


@app.post("/admin/ollama/test")
async def test_ollama_connection(
    body: dict,
    admin: User = Depends(require_admin),
):
    """Probe an Ollama instance and return its available model names."""
    url = (body.get("url") or OLLAMA_BASE_URL).strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="URL must start with http:// or https://")
    try:
        async with httpx.AsyncClient(timeout=8.0, verify=False) as client:
            resp = await client.get(f"{url}/api/tags")
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Ollama returned HTTP {resp.status_code}")
        data = resp.json()
        models = [m["name"] for m in data.get("models", [])]
        return {"ok": True, "models": models, "url": url}
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="Could not connect — is Ollama running?")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Connection timed out")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


def _get_docker_client():
    """Connect to Docker daemon via socket. Raises HTTP 503 if unavailable."""
    if not _DOCKER_SDK_AVAILABLE:
        raise HTTPException(status_code=503, detail="docker Python SDK not installed")
    try:
        return _docker_sdk.from_env()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Docker socket unavailable: {exc}")


def _find_ollama_container(client):
    """Find the Ollama container for this compose project. Returns None if not found."""
    try:
        containers = client.containers.list(
            all=True,
            filters={"label": [
                "com.docker.compose.service=ollama",
                f"com.docker.compose.project={COMPOSE_PROJECT_NAME}",
            ]},
        )
        if containers:
            return containers[0]
        # Fallback: any container whose name contains "ollama"
        for c in client.containers.list(all=True):
            if "ollama" in c.name.lower():
                return c
    except Exception:
        pass
    return None


@app.get("/admin/ollama/service")
async def get_ollama_service_status(admin: User = Depends(require_admin)):
    """Return the status of the Ollama Docker container."""
    if not _DOCKER_SDK_AVAILABLE:
        return {"status": "docker_unavailable", "detail": "docker SDK not installed"}
    try:
        client = _get_docker_client()
    except HTTPException:
        return {"status": "docker_unavailable", "detail": "Docker socket not mounted"}
    try:
        c = _find_ollama_container(client)
        if c is None:
            return {"status": "not_found", "container_name": None}
        c.reload()
        return {"status": c.status, "container_name": c.name, "id": c.short_id}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


@app.post("/admin/ollama/service/start")
async def start_ollama_service(admin: User = Depends(require_admin)):
    """Start (or create) the Ollama Docker container."""
    client = _get_docker_client()
    try:
        c = _find_ollama_container(client)
        if c is not None:
            c.reload()
            if c.status != "running":
                try:
                    c.start()
                    c.reload()
                except Exception as start_exc:
                    # Stale network reference (e.g. after docker compose down -v).
                    # Remove the broken container and fall through to recreate it.
                    if "network" in str(start_exc).lower():
                        logger.warning("Ollama container has stale network — removing and recreating: %s", start_exc)
                        try:
                            c.remove(force=True)
                        except Exception:
                            pass
                        c = None
                    else:
                        raise
            if c is not None:
                return {"ok": True, "action": "started", "container": c.name, "status": c.status}
        # Container doesn't exist (or was just removed due to stale network) — create it fresh
        volume_name = f"{COMPOSE_PROJECT_NAME}_ollama_data"
        try:
            client.volumes.get(volume_name)
        except Exception:
            client.volumes.create(volume_name)

        # Find the host project root by inspecting the app container's /app bind mount,
        # then look for certs/corporate-ca.pem so we can inject it into Ollama.
        ollama_volumes = {volume_name: {"bind": "/root/.ollama", "mode": "rw"}}
        ollama_env = {"OLLAMA_INSECURE": "true"}
        try:
            app_cs = client.containers.list(filters={"label": [
                "com.docker.compose.service=app",
                f"com.docker.compose.project={COMPOSE_PROJECT_NAME}",
            ]})
            if app_cs:
                for m in app_cs[0].attrs.get("Mounts", []):
                    if m.get("Type") == "bind" and m.get("Destination") == "/app":
                        host_project_root = os.path.dirname(m["Source"])
                        cert_host_path = os.path.join(host_project_root, "certs", "corporate-ca.pem")
                        ollama_volumes[cert_host_path] = {"bind": "/certs/corporate-ca.pem", "mode": "ro"}
                        ollama_env["SSL_CERT_FILE"] = "/certs/corporate-ca.pem"
                        break
        except Exception:
            pass

        c = client.containers.run(
            "ollama/ollama:latest",
            name=f"{COMPOSE_PROJECT_NAME}-ollama-1",
            detach=True,
            ports={"11434/tcp": 11434},
            volumes=ollama_volumes,
            environment=ollama_env,
            labels={
                "com.docker.compose.service": "ollama",
                "com.docker.compose.project": COMPOSE_PROJECT_NAME,
            },
            restart_policy={"Name": "unless-stopped"},
        )
        # Attach to the Compose project network so other services can reach
        # it via the "ollama" hostname. Find the network by inspecting the
        # app container — that's guaranteed to be on the right network.
        try:
            app_containers = client.containers.list(
                filters={"label": [
                    "com.docker.compose.service=app",
                    f"com.docker.compose.project={COMPOSE_PROJECT_NAME}",
                ]}
            )
            if app_containers:
                app_net_names = list(app_containers[0].attrs["NetworkSettings"]["Networks"].keys())
                if app_net_names:
                    client.networks.get(app_net_names[0]).connect(c, aliases=["ollama"])
        except Exception:
            pass  # non-fatal: container is running, just may not be on compose network
        return {"ok": True, "action": "created", "container": c.name, "status": c.status}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/admin/ollama/service/stop")
async def stop_ollama_service(admin: User = Depends(require_admin)):
    """Stop the Ollama Docker container."""
    client = _get_docker_client()
    try:
        c = _find_ollama_container(client)
        if c is None:
            raise HTTPException(status_code=404, detail="Ollama container not found")
        c.stop(timeout=10)
        return {"ok": True, "action": "stopped"}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/admin/ollama/pull")
async def pull_ollama_model(
    body: dict,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Stream model pull progress from the Ollama API."""
    model = (body.get("model") or "").strip()
    if not model:
        raise HTTPException(status_code=422, detail="Model name required")
    row = await db.get(AppSetting, "ollama_base_url")
    base_url = (row.value if row else OLLAMA_BASE_URL).rstrip("/")

    async def _stream():
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(600.0, connect=10.0), verify=False
            ) as client:
                async with client.stream(
                    "POST",
                    f"{base_url}/api/pull",
                    json={"model": model, "name": model, "stream": True},
                ) as resp:
                    logger.info("Ollama pull %s → HTTP %s", model, resp.status_code)
                    async for line in resp.aiter_lines():
                        if line:
                            logger.debug("Ollama pull line: %s", line[:120])
                            yield f"data: {line}\n\n"
                    logger.info("Ollama pull stream ended for %s", model)
        except httpx.ConnectError:
            yield 'data: {"status":"error","error":"Cannot connect to Ollama — is the service running?"}\n\n'
        except Exception as exc:
            yield f'data: {{"status":"error","error":"{str(exc)}"}}\n\n'
        yield 'data: {"status":"done"}\n\n'

    return StreamingResponse(_stream(), media_type="text/event-stream")


@app.delete("/admin/ollama/model")
async def delete_ollama_model(
    body: dict,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a local Ollama model."""
    model = (body.get("model") or "").strip()
    if not model:
        raise HTTPException(status_code=422, detail="Model name required")
    row = await db.get(AppSetting, "ollama_base_url")
    base_url = (row.value if row else OLLAMA_BASE_URL).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
            resp = await client.request(
                "DELETE",
                f"{base_url}/api/delete",
                json={"name": model},
            )
        if resp.status_code not in (200, 204):
            raise HTTPException(status_code=resp.status_code, detail=f"Ollama returned {resp.status_code}")
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Cannot connect to Ollama — is the service running?")
    return {"deleted": model}


# ── LLM Provider Key management ──────────────────────────────────────────────

@app.get("/admin/llm-provider-keys")
async def list_llm_provider_keys(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    out = []
    for p in KNOWN_PROVIDERS:
        row = await db.get(AppSetting, f"provider_key_{p['id']}")
        key_preview = ""
        if row:
            try:
                raw = decrypt(row.value)
                key_preview = f"…{raw[-4:]}" if len(raw) >= 4 else "set"
            except Exception:
                key_preview = "set"
        out.append({
            "provider": p["id"],
            "name": p["name"],
            "key_set": bool(row),
            "key_preview": key_preview,
        })
    return out


class LLMProviderKeyUpdate(BaseModel):
    provider: str
    key: str


@app.post("/admin/llm-provider-keys")
async def set_llm_provider_key(
    body: LLMProviderKeyUpdate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    valid_ids = {p["id"] for p in KNOWN_PROVIDERS}
    if body.provider not in valid_ids:
        raise HTTPException(status_code=422, detail=f"Unknown provider '{body.provider}'")
    if not body.key.strip():
        raise HTTPException(status_code=422, detail="Key cannot be empty")

    setting_key = f"provider_key_{body.provider}"
    enc = encrypt(body.key.strip())
    existing = await db.get(AppSetting, setting_key)
    if existing:
        existing.value = enc
    else:
        db.add(AppSetting(key=setting_key, value=enc))
    await db.commit()

    _SHARED_LLM_KEYS[body.provider] = body.key.strip()
    await _log_audit(db, admin.id, admin.email, "llm_provider_key_changed",
                     f"Provider key updated: {body.provider}")
    await db.commit()
    return {"ok": True}


@app.delete("/admin/llm-provider-keys/{provider}")
async def delete_llm_provider_key(
    provider: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    valid_ids = {p["id"] for p in KNOWN_PROVIDERS}
    if provider not in valid_ids:
        raise HTTPException(status_code=422, detail=f"Unknown provider '{provider}'")

    setting_key = f"provider_key_{provider}"
    existing = await db.get(AppSetting, setting_key)
    if existing:
        await db.delete(existing)
        await db.commit()

    _SHARED_LLM_KEYS[provider] = os.getenv(
        next(p["env"] for p in KNOWN_PROVIDERS if p["id"] == provider), ""
    )
    await _log_audit(db, admin.id, admin.email, "llm_provider_key_removed",
                     f"Provider key removed: {provider}")
    await db.commit()
    return {"ok": True}


class TestEmailRequest(BaseModel):
    to: str


@app.post("/admin/test-smtp")
async def test_smtp_connection(
    body: dict,
    admin: User = Depends(require_admin),
):
    """Test SMTP connection with inline credentials — does not save settings."""
    host = (body.get("smtp_host") or "").strip()
    port = int(body.get("smtp_port") or 587)
    username = (body.get("email_username") or "").strip()
    password = (body.get("email_password") or "").strip()

    if not host:
        raise HTTPException(status_code=422, detail="SMTP host is required")

    def _test():
        smtp = smtplib.SMTP(host, port, timeout=10)
        try:
            smtp.ehlo()
            smtp.starttls()
            if username and password:
                smtp.login(username, password)
        finally:
            try:
                smtp.quit()
            except Exception:
                pass

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, _test)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True}


@app.post("/admin/test-email")
async def send_test_email(
    body: TestEmailRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Send a test email to verify SMTP configuration."""
    cfg = await _get_email_config(db)
    if not cfg["smtp_host"]:
        raise HTTPException(status_code=503, detail="SMTP host is not configured")

    to = body.to.strip()
    if not to or "@" not in to:
        raise HTTPException(status_code=422, detail="Invalid recipient email address")

    def _send_test() -> None:
        msg = email.mime.multipart.MIMEMultipart("alternative")
        msg["Subject"] = "HGA Prompt Demo — Test Email"
        msg["From"]    = cfg["from_email"]
        msg["To"]      = to

        plain = (
            "This is a test email from HGA Prompt Demo.\n"
            "Your email settings are configured correctly."
        )
        html = (
            "<!DOCTYPE html><html><body style='font-family:Arial,sans-serif'>"
            "<p>This is a test email from <strong>HGA Prompt Demo</strong>.</p>"
            "<p>Your email settings are configured correctly.</p>"
            "</body></html>"
        )
        msg.attach(email.mime.text.MIMEText(plain, "plain"))
        msg.attach(email.mime.text.MIMEText(html, "html"))

        smtp = smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=10)
        try:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(cfg["email_username"], cfg["email_password"])
            smtp.sendmail(cfg["from_email"], to, msg.as_string())
        finally:
            try:
                smtp.quit()
            except Exception:
                pass  # server may close connection before QUIT — email was already sent

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, _send_test)
    except Exception as exc:
        logger.error("Test email send failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))

    await _log_audit(db, admin.id, admin.email, "email_settings_tested", f"Test email sent to {to}")

    return {"sent": True}


# ── Email verification ────────────────────────────────────────────────────────

def _send_verification_email(to_email: str, code: str, guest_name: str, config: dict) -> None:
    """Blocking SMTP send — run in a thread executor."""
    msg = email.mime.multipart.MIMEMultipart("alternative")
    msg["Subject"] = "Your HGA Prompt Demo verification code"
    msg["From"]    = f"HGA Prompt Demo <{config['from_email']}>"
    msg["To"]      = to_email

    plain = (
        f"Hi {guest_name or 'there'},\n\n"
        f"Your verification code is: {code}\n\n"
        f"This code expires in 10 minutes.\n\n"
        f"If you didn't request this, you can safely ignore this email.\n\n"
        f"— HGA Prompt Demo"
    )

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0f1117;font-family:'Inter',Arial,sans-serif">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0f1117;padding:40px 0">
    <tr><td align="center">
      <table width="480" cellpadding="0" cellspacing="0"
             style="background:#1a1d27;border-radius:14px;border:1px solid #2a2d3e;overflow:hidden">

        <!-- Header -->
        <tr>
          <td style="background:linear-gradient(135deg,#6c63ff 0%,#5a52e0 100%);padding:28px 36px;text-align:center">
            <img src="https://assets-global.website-files.com/656f4138f2ff78452cf12053/658da588005946f4a6cbd84e_webpac.png" width="32" height="32"
                 style="border-radius:6px;margin-bottom:10px;display:block;margin-left:auto;margin-right:auto"
                 alt="HGA">
            <div style="color:#fff;font-size:20px;font-weight:700;letter-spacing:-0.3px">HGA Prompt Demo</div>
            <div style="color:rgba(255,255,255,0.7);font-size:13px;margin-top:4px">Email Verification</div>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="padding:36px 36px 28px">
            <p style="color:#e8eaf6;font-size:15px;margin:0 0 8px">
              Hi <strong>{guest_name or 'there'}</strong>,
            </p>
            <p style="color:#7b80a8;font-size:13px;line-height:1.6;margin:0 0 28px">
              Use the code below to verify your email address and complete setup.
              This code expires in <strong style="color:#e8eaf6">10 minutes</strong>.
            </p>

            <!-- Code box -->
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr><td align="center" style="padding:4px 0 28px">
                <div style="display:inline-block;background:#20253a;border:2px solid #6c63ff;
                            border-radius:12px;padding:20px 36px;letter-spacing:14px;
                            font-size:36px;font-weight:800;color:#fff;font-family:monospace">
                  {code}
                </div>
              </td></tr>
            </table>

            <p style="color:#7b80a8;font-size:12px;line-height:1.6;margin:0">
              If you didn't request this code, you can safely ignore this email.
              Someone may have entered your address by mistake.
            </p>
          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="padding:16px 36px 24px;border-top:1px solid #2a2d3e;text-align:center">
            <p style="color:#4a4f6a;font-size:11px;margin:0">
              Sent by HGA Prompt Demo &nbsp;·&nbsp; Powered by Prompt Security
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""

    msg.attach(email.mime.text.MIMEText(plain, "plain"))
    msg.attach(email.mime.text.MIMEText(html, "html"))

    smtp = smtplib.SMTP(config["smtp_host"], config["smtp_port"], timeout=10)
    try:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(config["email_username"], config["email_password"])
        smtp.sendmail(config["from_email"], to_email, msg.as_string())
    finally:
        try:
            smtp.quit()
        except Exception:
            pass  # server may close connection before QUIT — email was already sent


class EmailCodeRequest(BaseModel):
    email: str
    name: Optional[str] = ""


class EmailCodeVerify(BaseModel):
    email: str
    code: str


@app.post("/guest/request-email-code")
async def request_email_code(body: EmailCodeRequest, db: AsyncSession = Depends(get_db)):
    """Send a 4-digit verification code to the given email address."""
    cfg = await _get_email_config(db)
    if not cfg["smtp_host"]:
        raise HTTPException(status_code=503, detail="Email service not configured")
    to = body.email.strip().lower()
    if not to or "@" not in to:
        raise HTTPException(status_code=422, detail="Invalid email address")

    code = f"{random.randint(0, 9999):04d}"
    _email_verify_codes[to] = {"code": code, "expires_at": time.time() + _EMAIL_CODE_TTL}

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, _send_verification_email, to, code, body.name or "", cfg)
    except Exception as exc:
        logger.error("Failed to send verification email to %s: %s", to, exc)
        raise HTTPException(status_code=502, detail="Failed to send email. Please check your address and try again.")

    return {"sent": True}


@app.post("/guest/verify-email-code")
async def verify_email_code(body: EmailCodeVerify):
    """Verify the 4-digit code sent to an email address."""
    to = body.email.strip().lower()
    entry = _email_verify_codes.get(to)
    if not entry:
        return {"valid": False, "reason": "no_code"}
    if time.time() > entry["expires_at"]:
        _email_verify_codes.pop(to, None)
        return {"valid": False, "reason": "expired"}
    if entry["code"] != body.code.strip():
        return {"valid": False, "reason": "wrong_code"}
    _email_verify_codes.pop(to, None)
    return {"valid": True}


# ── Guest endpoints (open mode — no auth) ────────────────────────────────────

class GuestPSConfig(BaseModel):
    base_url: Optional[str] = None
    gateway_url: Optional[str] = None
    app_id: Optional[str] = None
    mode: str = "api"
    enabled: bool = True

class GuestChatRequest(BaseModel):
    messages: list[ChatMessage]
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    skip_ps: bool = False
    guest_name: Optional[str] = None
    guest_email: Optional[str] = None
    ps_config: Optional[GuestPSConfig] = None
    session_id: Optional[str] = None


@app.get("/guest/models")
async def guest_models():
    live = _model_cache or await refresh_model_cache()
    available = live if live else _FALLBACK_MODELS
    shared_providers = {k for k, v in _SHARED_LLM_KEYS.items() if v}
    enriched = []
    for m in available:
        meta = _model_meta(m["id"])
        provider = _detect_provider(m["id"])
        key_set = provider == "ollama" or provider in shared_providers
        enriched.append({**m, **meta, "key_set": key_set})
    return {"models": enriched, "fallback": not bool(live)}


@app.get("/guest/ps-tenants", response_model=list[PSTenantOut])
async def guest_ps_tenants(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PSTenant).order_by(PSTenant.name))
    return result.scalars().all()


_GUEST_LOG_ALLOWED = {"ps_config_changed", "scenario_created", "scenario_updated", "scenario_deleted", "guest_registered"}
_USER_LOG_ALLOWED  = {"scenario_created", "scenario_updated", "scenario_deleted"}

@app.post("/users/log-event", status_code=204)
async def user_log_event(
    body: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    event_type = (body.get("event_type") or "").strip()
    if event_type not in _USER_LOG_ALLOWED:
        return
    detail = (body.get("detail") or "")[:8000]
    await _log_audit(db, current_user.id, current_user.email, event_type, detail)


@app.post("/guest/log-event", status_code=204)
async def guest_log_event(body: dict, http_request: Request, db: AsyncSession = Depends(get_db)):
    event_type = (body.get("event_type") or "").strip()
    if event_type not in _GUEST_LOG_ALLOWED:
        return
    detail      = (body.get("detail")       or "")[:8000]
    guest_name  = (body.get("guest_name")   or "Guest")[:120]
    guest_email = (body.get("guest_email")  or "").strip().lower()[:255]
    ip          = http_request.client.host if http_request.client else "unknown"
    if guest_email and guest_name:
        user_email = f"{guest_email} ({guest_name})"
    elif guest_email:
        user_email = guest_email
    else:
        user_email = f"{guest_name} ({ip}) [open mode]"
    await _log_audit(db, None, user_email, event_type, detail)


@app.post("/guest/chat/stream")
async def guest_chat_stream(
    request: GuestChatRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
):
    if not request.messages:
        raise HTTPException(status_code=400, detail="messages list cannot be empty")

    available = _model_cache or _FALLBACK_MODELS
    model = request.model or (available[0]["id"] if available else None)
    if not model:
        raise HTTPException(status_code=503, detail="No models available")

    last_user_msg = next((m.content for m in reversed(request.messages) if m.role == "user"), None)
    if not last_user_msg:
        raise HTTPException(status_code=400, detail="No user message found")

    # Build guest identifier from email, name, and IP
    local_ip = http_request.client.host if http_request.client else "unknown"
    forwarded = http_request.headers.get("X-Forwarded-For", "")
    public_ip = forwarded.split(",")[0].strip() if forwarded else local_ip
    ip_label = f"{public_ip}" if public_ip == local_ip else f"{public_ip} / {local_ip}"
    name  = (request.guest_name  or "").strip()
    email = (request.guest_email or "").strip().lower()
    # Audit identifier: "email (name)" when both present, fallback to name or IP
    if email and name:
        guest_id = f"{email} ({name})"
    elif email:
        guest_id = email
    elif name:
        guest_id = name
    else:
        guest_id = f"guest:{ip_label}"
    # Username sent to Prompt Security: prefer email, fall back to name/ip
    ps_user = email or name or f"guest:{ip_label}"

    # Ensure a ChatSession exists for this guest
    session_id = request.session_id or str(uuid.uuid4())
    session = await db.scalar(select(ChatSession).where(ChatSession.id == session_id))
    if not session:
        title = last_user_msg[:60] if last_user_msg else "Guest Chat"
        session = ChatSession(id=session_id, user_id=None, guest_id=guest_id, title=title)
        db.add(session)
        await db.flush()
    # Persist the user message (only the last one, to avoid re-saving history on each turn)
    db.add(Message(
        session_id=session_id, user_id=None, guest_id=guest_id,
        role="user", content=last_user_msg, model=model,
    ))
    await db.commit()

    system_prompt = request.system_prompt or "You are a helpful AI assistant."

    cfg = request.ps_config
    ps_client: Optional[PromptSecurityClient] = None
    ps_gw_client: Optional[AsyncOpenAI] = None
    ps_mode = cfg.mode if cfg else "api"

    if cfg and cfg.enabled and cfg.base_url and cfg.app_id and not request.skip_ps:
        if ps_mode == "api":
            ps_client = PromptSecurityClient(base_url=cfg.base_url, app_id=cfg.app_id)
        elif ps_mode == "gateway" and cfg.gateway_url:
            gw_host = cfg.gateway_url.rstrip("/")
            gw_base = gw_host if gw_host.endswith("/v1") else gw_host + "/v1"
            provider = _detect_provider(model)
            llm_key = _SHARED_LLM_KEYS.get(provider, "")
            if llm_key:
                ps_gw_client = AsyncOpenAI(
                    api_key=llm_key,
                    base_url=gw_base,
                    timeout=30.0,
                    default_headers={"ps-app-id": cfg.app_id},
                )

    async def _persist_guest(content: str, action: str, scanned: bool, violations: list, elapsed_ms: int,
                             prompt_tokens: Optional[int] = None, completion_tokens: Optional[int] = None,
                             total_tokens: Optional[int] = None):
        try:
            ps_tag = f" [PS:{action}]" if scanned else ""
            detail = f"{model}{ps_tag} [{ip_label}] — {last_user_msg[:80]}"
            async with _db_module.AsyncSessionLocal() as log_db:
                log_db.add(Message(
                    session_id=session_id, user_id=None, guest_id=guest_id,
                    role="assistant", content=content, model=model,
                    ps_scanned=scanned, ps_action=action,
                    ps_violations=violations,
                    response_ms=elapsed_ms,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                ))
                log_db.add(AuditEvent(
                    user_id=None, user_email=guest_id,
                    event_type="guest_chat", detail=detail[:500],
                ))
                await log_db.commit()
        except Exception as log_err:
            logger.warning("Guest activity log failed: %s", log_err)

    async def generate():
        reply = ""
        prompt_action = "pass"
        t0 = time.monotonic()
        prompt_tokens: Optional[int] = None
        completion_tokens: Optional[int] = None
        total_tokens: Optional[int] = None
        ps_prompt_raw: Optional[dict] = None
        ps_resp_raw: Optional[dict] = None

        payload = [{"role": "system", "content": system_prompt}] + [
            {"role": m.role, "content": _build_content(m)} for m in request.messages
        ]

        # Gateway mode
        if ps_gw_client:
            try:
                prompt_tokens = estimate_message_tokens(payload, model=model)
                stream = await ps_gw_client.chat.completions.create(
                    model=model, messages=payload, stream=True
                )
                async for chunk in stream:
                    if getattr(chunk, "usage", None):
                        prompt_tokens = getattr(chunk.usage, "prompt_tokens", prompt_tokens)
                        completion_tokens = getattr(chunk.usage, "completion_tokens", completion_tokens)
                        total_tokens = getattr(chunk.usage, "total_tokens", total_tokens)
                    if chunk.choices and chunk.choices[0].delta.content:
                        token = chunk.choices[0].delta.content
                        reply += token
                        yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
            except openai.BadRequestError as e:
                body_str = str(e).lower()
                elapsed = int((time.monotonic() - t0) * 1000)
                if any(w in body_str for w in ("block", "policy", "violat", "denied")):
                    yield f"data: {json.dumps({'type': 'blocked', 'action': 'block', 'violations': []})}\n\n"
                    await _persist_guest("[BLOCKED by Prompt Security Gateway]", "block", True, [], elapsed)
                else:
                    yield f"data: {json.dumps({'type': 'error', 'detail': f'Gateway error: {e}'})}\n\n"
                return
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'detail': f'Gateway error: {e}'})}\n\n"
                return
            elapsed = int((time.monotonic() - t0) * 1000)
            completion_tokens = completion_tokens or estimate_text_tokens(reply, model=model)
            prompt_tokens = prompt_tokens or estimate_message_tokens(payload, model=model)
            total_tokens = total_tokens or (prompt_tokens + completion_tokens)
            yield f"data: {json.dumps({'type': 'done', 'model': model, 'ps_scanned': True, 'ps_action': 'gateway', 'ps_violations': [], 'messages_today': 0, 'daily_limit': None, 'prompt_tokens': prompt_tokens, 'completion_tokens': completion_tokens, 'total_tokens': total_tokens})}\n\n"
            await _persist_guest(reply, "gateway", True, [], elapsed, prompt_tokens, completion_tokens, total_tokens)
            return

        # API mode: PS prompt scan
        if ps_client:
            try:
                ps_result = await ps_client.protect_prompt(
                    user_prompt=last_user_msg,
                    system_prompt=system_prompt,
                    user=ps_user,
                )
                ps_prompt_raw = {"request": ps_result.raw_request, "response": ps_result.raw}
                if not ps_result.allowed:
                    elapsed = int((time.monotonic() - t0) * 1000)
                    yield f"data: {json.dumps({'type': 'blocked', 'action': 'block', 'violations': ps_result.violations, 'ps_raw': {'prompt': ps_prompt_raw}})}\n\n"
                    await _persist_guest("[BLOCKED by Prompt Security]", "block", True, ps_result.violations, elapsed)
                    return
                if ps_result.modified_text:
                    prompt_action = "modify"
                    for i in range(len(payload) - 1, -1, -1):
                        if payload[i]["role"] == "user":
                            payload[i]["content"] = ps_result.modified_text
                            break
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'detail': f'PS scan failed: {e}'})}\n\n"
                return

        # LLM stream
        try:
            prompt_tokens = estimate_message_tokens(payload, model=model)
            stream_kwargs = {"model": model, "messages": payload, "stream": True, "stream_options": {"include_usage": True},
                             "extra_body": _litellm_extra(model)}
            stream = await litellm_client.chat.completions.create(**stream_kwargs)
            async for chunk in stream:
                if getattr(chunk, "usage", None):
                    prompt_tokens = getattr(chunk.usage, "prompt_tokens", prompt_tokens)
                    completion_tokens = getattr(chunk.usage, "completion_tokens", completion_tokens)
                    total_tokens = getattr(chunk.usage, "total_tokens", total_tokens)
                if chunk.choices and chunk.choices[0].delta.content:
                    token = chunk.choices[0].delta.content
                    reply += token
                    yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
        except Exception as e:
            logger.error("Guest LLM stream error: %s", e)
            yield f"data: {json.dumps({'type': 'error', 'detail': str(e)})}\n\n"
            return

        # API mode: PS response scan
        ps_violations: list = []
        if ps_client and reply:
            try:
                ps_resp = await ps_client.protect_response(
                    response_text=reply,
                    user_prompt=last_user_msg,
                    system_prompt=system_prompt,
                    user=ps_user,
                )
                ps_resp_raw = {"request": ps_resp.raw_request, "response": ps_resp.raw}
                ps_violations = ps_resp.violations
                if not ps_resp.allowed:
                    elapsed = int((time.monotonic() - t0) * 1000)
                    yield f"data: {json.dumps({'type': 'revoke', 'action': 'block', 'violations': ps_violations, 'ps_raw': {'prompt': ps_prompt_raw, 'response': ps_resp_raw}})}\n\n"
                    await _persist_guest("[RESPONSE BLOCKED by Prompt Security]", "block", True, ps_violations, elapsed)
                    return
                if ps_resp.modified_text:
                    reply = ps_resp.modified_text
                    prompt_action = "modify"
                    yield f"data: {json.dumps({'type': 'sanitized', 'text': reply})}\n\n"
            except Exception as e:
                logger.warning("Guest PS response scan failed: %s", e)

        elapsed = int((time.monotonic() - t0) * 1000)
        completion_tokens = completion_tokens or estimate_text_tokens(reply, model=model)
        prompt_tokens = prompt_tokens or estimate_message_tokens(payload, model=model)
        total_tokens = total_tokens or (prompt_tokens + completion_tokens)
        ps_active = bool(ps_client)
        ps_raw_payload = {"prompt": ps_prompt_raw, "response": ps_resp_raw} if ps_active else None
        yield f"data: {json.dumps({'type': 'done', 'model': model, 'ps_scanned': ps_active, 'ps_action': prompt_action, 'ps_violations': ps_violations, 'messages_today': 0, 'daily_limit': None, 'prompt_tokens': prompt_tokens, 'completion_tokens': completion_tokens, 'total_tokens': total_tokens, 'ps_raw': ps_raw_payload})}\n\n"
        await _persist_guest(reply, prompt_action, ps_active, ps_violations, elapsed, prompt_tokens, completion_tokens, total_tokens)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Helpers ───────────────────────────────────────────────────────────────────
def _user_out(user: User) -> UserOut:
    llm_providers: list[str] = []
    if user.llm_api_keys_enc:
        try:
            llm_providers = list(json.loads(decrypt(user.llm_api_keys_enc)).keys())
        except Exception:
            pass
    return UserOut(
        id=user.id,
        email=user.email,
        role=user.role,
        is_active=user.is_active,
        daily_message_limit=user.daily_message_limit,
        allowed_models=user.allowed_models,
        ps_tenant_id=user.ps_tenant_id,
        ps_tenant=PSTenantOut.model_validate(user.ps_tenant) if user.ps_tenant else None,
        ps_configured=bool(user.ps_tenant_id and user.ps_api_key_enc),
        ps_mode=user.ps_mode,
        ps_enabled=user.ps_enabled,
        llm_key_settings_visible=SHOW_LLM_KEY_SETTINGS,
        llm_keys_configured=llm_providers,
        must_change_password=user.must_change_password,
        created_at=user.created_at,
    )


def _api_key_out(key: APIKey) -> APIKeyOut:
    return APIKeyOut(
        id=key.id,
        name=key.name,
        key_preview=f"{key.key_prefix}…",
        is_active=key.is_active,
        last_used_at=key.last_used_at,
        created_at=key.created_at,
    )


def _build_content(m: ChatMessage):
    if m.image_url:
        parts = [{"type": "image_url", "image_url": {"url": m.image_url}}]
        if m.content:
            parts.append({"type": "text", "text": m.content})
        return parts
    return m.content


def _ensure_public_api_enabled():
    if not PUBLIC_API_ENABLED:
        raise HTTPException(
            status_code=403,
            detail="Public API is disabled. Set PUBLIC_API_ENABLED=true to use /v1/responses.",
        )


_PRIVATE_IP_PREFIXES = (
    "10.", "172.16.", "172.17.", "172.18.", "172.19.", "172.20.", "172.21.",
    "172.22.", "172.23.", "172.24.", "172.25.", "172.26.", "172.27.", "172.28.",
    "172.29.", "172.30.", "172.31.", "192.168.", "127.", "169.254.",
)
_UNSAFE_HOSTNAMES = {"localhost", ""}


def _is_unsafe_host(hostname: str) -> bool:
    if hostname in _UNSAFE_HOSTNAMES:
        return True
    if hostname.endswith(".local"):
        return True
    return any(hostname.startswith(pfx) for pfx in _PRIVATE_IP_PREFIXES)


def _validate_external_https_url(url: str, field_name: str) -> str:
    """Validate that a URL is HTTPS and targets a public hostname. Raises HTTPException if not."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise HTTPException(status_code=422, detail=f"{field_name}: URL must use HTTPS")
    if _is_unsafe_host(parsed.hostname or ""):
        raise HTTPException(status_code=422, detail=f"{field_name}: URL must target a public hostname")
    return url


def _normalize_legacy_public_http_url(url: str) -> Optional[str]:
    """Upgrade http:// to https:// only for public hostnames. Returns None for private/localhost."""
    parsed = urlparse(url)
    if parsed.scheme != "http":
        return url
    if _is_unsafe_host(parsed.hostname or ""):
        return None
    return urlunparse(parsed._replace(scheme="https"))


async def _migrate_legacy_ps_tenant_urls(db: AsyncSession) -> None:
    """Disable ps_enabled for users whose PS tenant has an invalid (non-HTTPS/private) base_url."""
    result = await db.execute(
        select(User).where(User.ps_enabled.is_(True)).options(selectinload(User.ps_tenant))
    )
    users = result.scalars().all()
    for user in users:
        if user.ps_tenant and _is_unsafe_host(urlparse(user.ps_tenant.base_url).hostname or ""):
            user.ps_enabled = False
            logger.warning("Disabled PS for user %s — invalid tenant base_url: %s", user.email, user.ps_tenant.base_url)
    await db.commit()


def _build_ps_api_client(user: User) -> Optional[PromptSecurityClient]:
    if not (user.ps_enabled and user.ps_tenant and user.ps_api_key_enc):
        return None
    parsed = urlparse(user.ps_tenant.base_url)
    if parsed.scheme != "https" or _is_unsafe_host(parsed.hostname or ""):
        logger.warning("PS tenant base_url is invalid for %s: %s", user.email, user.ps_tenant.base_url)
        return None
    try:
        ps_app_id = decrypt(user.ps_api_key_enc)
    except ValueError:
        logger.warning("PS key decrypt failed for %s", user.email)
        return None
    if not ps_app_id:
        return None
    return PromptSecurityClient(base_url=user.ps_tenant.base_url, app_id=ps_app_id)


def _extract_response_text(resp) -> str:
    text = getattr(resp, "output_text", None)
    if text:
        return text

    choices = getattr(resp, "choices", None) or []
    if choices:
        msg = getattr(choices[0], "message", None)
        if msg and getattr(msg, "content", None):
            return msg.content
    return ""


async def _log_audit(
    db: AsyncSession,
    user_id: Optional[int],
    user_email: str,
    event_type: str,
    detail: Optional[str] = None,
):
    ev = AuditEvent(user_id=user_id, user_email=user_email, event_type=event_type, detail=detail)
    db.add(ev)
    await db.commit()


async def _log_msg(
    db: AsyncSession,
    session_id: str,
    user_id: Optional[int],
    role: str,
    content: str,
    model: Optional[str],
    ps_scanned: bool = False,
    ps_blocked: bool = False,
    ps_action: str = "pass",
    ps_violations: Optional[list] = None,
    response_ms: Optional[int] = None,
    guest_id: Optional[str] = None,
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
):
    msg = Message(
        session_id=session_id,
        user_id=user_id,
        guest_id=guest_id,
        role=role,
        content=content,
        model=model,
        ps_scanned=ps_scanned,
        ps_action=ps_action if not ps_blocked else "block",
        ps_violations=ps_violations or [],
        response_ms=response_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )
    db.add(msg)
    await db.commit()


# ── Demo Scenarios ────────────────────────────────────────────────────────────

@app.get("/demo-scenarios")
async def list_demo_scenarios_public(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(DemoScenario)
        .where(DemoScenario.is_active == True)
        .order_by(DemoScenario.sort_order, DemoScenario.id)
    )
    scenarios = result.scalars().all()
    return [
        {
            "id": s.id, "key": s.key, "title": s.title, "category": s.category,
            "severity": s.severity, "prompt": s.prompt, "expected_action": s.expected_action,
            "description": s.description, "attacker_goal": s.attacker_goal,
            "why_caught": s.why_caught, "talking_point": s.talking_point,
            "entities": s.entities or [], "meta": s.meta or {},
            "sort_order": s.sort_order,
        }
        for s in scenarios
    ]


@app.get("/admin/demo-scenarios")
async def list_demo_scenarios_admin(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(DemoScenario).order_by(DemoScenario.sort_order, DemoScenario.id)
    )
    scenarios = result.scalars().all()
    return [
        {
            "id": s.id, "key": s.key, "title": s.title, "category": s.category,
            "severity": s.severity, "prompt": s.prompt, "expected_action": s.expected_action,
            "description": s.description, "attacker_goal": s.attacker_goal,
            "why_caught": s.why_caught, "talking_point": s.talking_point,
            "entities": s.entities or [], "meta": s.meta or {},
            "sort_order": s.sort_order, "is_active": s.is_active,
        }
        for s in scenarios
    ]


@app.post("/admin/demo-scenarios", status_code=201)
async def create_demo_scenario(
    body: dict,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    key = body.get("key") or body.get("title", "").lower().replace(" ", "_")[:60]
    s = DemoScenario(
        key=key, title=body.get("title", ""), category=body.get("category", ""),
        severity=body.get("severity", "MEDIUM"), prompt=body.get("prompt", ""),
        expected_action=body.get("expected_action", "block"),
        description=body.get("description"), attacker_goal=body.get("attacker_goal"),
        why_caught=body.get("why_caught"), talking_point=body.get("talking_point"),
        entities=body.get("entities") or None, meta=body.get("meta") or None,
        sort_order=int(body.get("sort_order", 0)), is_active=body.get("is_active", True),
    )
    db.add(s)
    await db.commit()
    await db.refresh(s)
    await _log_audit(db, admin.id, admin.email, "scenario_created", f"'{s.title}' · {s.category} · {s.severity}")
    return {"id": s.id, "key": s.key}


@app.patch("/admin/demo-scenarios/{scenario_id}")
async def update_demo_scenario(
    scenario_id: int,
    body: dict,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    s = await db.get(DemoScenario, scenario_id)
    if not s:
        raise HTTPException(status_code=404, detail="Scenario not found")
    for field in ("title", "category", "severity", "prompt", "expected_action",
                  "description", "attacker_goal", "why_caught", "talking_point",
                  "sort_order", "is_active"):
        if field in body:
            setattr(s, field, body[field])
    if "entities" in body:
        s.entities = body["entities"] or None
    if "meta" in body:
        s.meta = body["meta"] or None
    await db.commit()
    await _log_audit(db, admin.id, admin.email, "scenario_updated", f"'{s.title}' · {s.category}")
    return {"ok": True}


@app.delete("/admin/demo-scenarios/{scenario_id}", status_code=204)
async def delete_demo_scenario(
    scenario_id: int,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    s = await db.get(DemoScenario, scenario_id)
    if not s:
        raise HTTPException(status_code=404, detail="Scenario not found")
    await _log_audit(db, admin.id, admin.email, "scenario_deleted", f"'{s.title}' · {s.category}")
    await db.delete(s)
    await db.commit()


@app.post("/admin/demo-scenarios/save-master", status_code=200)
async def save_master_scenarios(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(DemoScenario).order_by(DemoScenario.sort_order, DemoScenario.id)
    )
    scenarios = result.scalars().all()
    data = [
        {k: v for k, v in {
            "key": s.key,
            "title": s.title,
            "category": s.category,
            "severity": s.severity,
            "expected_action": s.expected_action,
            "sort_order": s.sort_order,
            "description": s.description,
            "attacker_goal": s.attacker_goal,
            "why_caught": s.why_caught,
            "talking_point": s.talking_point,
            "entities": s.entities,
            "meta": s.meta,
            "prompt": s.prompt,
        }.items() if v is not None}
        for s in scenarios
    ]
    try:
        os.makedirs(os.path.dirname(_SCENARIOS_FILE), exist_ok=True)
        with open(_SCENARIOS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write scenarios.json: {e}")
    await _log_audit(db, admin.id, admin.email, "scenarios_master_saved", f"{len(data)} scenarios written to scenarios.json")
    return {"saved": len(data)}
