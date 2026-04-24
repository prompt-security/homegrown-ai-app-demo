import asyncio
import base64
import io
import json
import logging
import os
import secrets
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import httpx
import openai
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

load_dotenv()

from auth import (
    create_access_token, create_api_key, get_current_api_key, get_current_user,
    hash_api_key, hash_password, require_admin, verify_password,
)
from crypto import decrypt, encrypt
from database import AsyncSessionLocal, Base, engine, get_db
from models import APIKey, AuditEvent, ChatSession, Message, PSTenant, User
from prompt_security import PromptSecurityClient
from schemas import (
    APIKeyCreateRequest, APIKeyCreateResponse, APIKeyOut,
    ChatMessage, ChatRequest, ChatResponse,
    LLMKeysUpdate, LoginRequest, MessageOut, PSConfigUpdate, PSTenantCreate, PSTenantOut, PSTenantUpdate,
    PublicResponseOut, PublicResponseOutput, PublicResponseRequest, PublicResponseUsage,
    SessionOut, TokenEstimateResponse, TokenResponse, UserCreate, UserOut, UserStats, UserUpdate,
)
from token_counter import estimate_message_tokens, estimate_text_tokens

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(name)s  %(message)s")
logger = logging.getLogger("main")

# ── Configuration ─────────────────────────────────────────────────────────────
LITELLM_BASE_URL   = os.getenv("LITELLM_BASE_URL", "http://litellm:4000")
LITELLM_MASTER_KEY = os.getenv("LITELLM_MASTER_KEY", "")
ADMIN_EMAIL        = os.getenv("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD     = os.getenv("ADMIN_PASSWORD", "admin")
DEFAULT_DAILY_LIMIT = int(os.getenv("DEFAULT_DAILY_LIMIT", "50")) or None
PUBLIC_API_ENABLED = os.getenv("PUBLIC_API_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
PUBLIC_API_MAX_PROMPT_TOKENS = int(os.getenv("PUBLIC_API_MAX_PROMPT_TOKENS", "4000"))
PUBLIC_API_MAX_OUTPUT_TOKENS = int(os.getenv("PUBLIC_API_MAX_OUTPUT_TOKENS", "600"))
PUBLIC_API_ALLOW_SYSTEM_PROMPT = os.getenv("PUBLIC_API_ALLOW_SYSTEM_PROMPT", "false").lower() in {"1", "true", "yes", "on"}
SHOW_LLM_KEY_SETTINGS = os.getenv("SHOW_LLM_KEY_SETTINGS", "false").lower() in {"1", "true", "yes", "on"}
APP_ENV = os.getenv("APP_ENV", os.getenv("ENV", "development")).lower()

# Shared LLM keys (fallback when user has no per-provider key)
_SHARED_LLM_KEYS = {
    "openai":      os.getenv("OPENAI_API_KEY", ""),
    "anthropic":   os.getenv("ANTHROPIC_API_KEY", ""),
    "google":      os.getenv("GOOGLE_API_KEY", ""),
    "perplexity":  os.getenv("PERPLEXITY_API_KEY", ""),
    "openrouter":  os.getenv("OPENROUTER_API_KEY", ""),
}

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

# ── Provider base URLs for per-user direct calls ────────────────────────────
_PROVIDER_URLS = {
    "openai":     "https://api.openai.com/v1",
    "anthropic":  "https://api.anthropic.com/v1",
    "google":     "https://generativelanguage.googleapis.com/v1beta/openai/",
    "openrouter": "https://openrouter.ai/api/v1",
}


def _detect_provider(model_id: str) -> str:
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
    is_free = model_id.lower().endswith(":free")
    return {
        "category": "free" if is_free else "paid",
        "provider": {"openai": "OpenAI", "anthropic": "Anthropic", "google": "Google", "perplexity": "Perplexity", "openrouter": "OpenRouter"}[provider],
        "requires_key": None if is_free else provider,
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
    """Fail fast in non-dev environments when insecure defaults are used."""
    if APP_ENV in {"dev", "development", "test", "local"}:
        return

    problems = []
    secret_key = os.getenv("SECRET_KEY", "")
    if not secret_key or secret_key in {"dev_secret_change_me", "change_me", "changeme", "test-secret-key-for-unit-tests"}:
        problems.append("SECRET_KEY must be explicitly set to a strong value")

    admin_password = os.getenv("ADMIN_PASSWORD", "")
    if not admin_password or admin_password in {"admin", "change_me", "changeme", "password"}:
        problems.append("ADMIN_PASSWORD must not use insecure defaults")

    if problems:
        raise RuntimeError("Insecure bootstrap configuration: " + "; ".join(problems))


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


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    _validate_security_bootstrap_config()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as db:
        existing = await db.scalar(select(User).where(User.email == ADMIN_EMAIL))
        if not existing:
            admin = User(
                email=ADMIN_EMAIL,
                hashed_password=hash_password(ADMIN_PASSWORD),
                role="admin",
                is_active=True,
            )
            db.add(admin)
            await db.commit()
            logger.info("Bootstrap admin created: %s", ADMIN_EMAIL)

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
    return HTMLResponse(open("static/admin.html").read())


# ── Auth ──────────────────────────────────────────────────────────────────────
@app.post("/auth/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(User).where(User.email == body.email, User.is_active == True)
        .options(selectinload(User.ps_tenant))
    )
    user = result.scalar_one_or_none()
    if not user or not user.hashed_password or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token({"sub": str(user.id)})
    return TokenResponse(
        access_token=token,
        user=_user_out(user),
    )

@app.get("/auth/me", response_model=UserOut)
async def me(current_user: User = Depends(get_current_user)):
    return _user_out(current_user)


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
        key_set = provider in user_providers or provider in shared_providers
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
        resp = await llm.chat.completions.create(
            model=effective_model,
            messages=payload,
            max_tokens=PUBLIC_API_MAX_OUTPUT_TOKENS,
        )
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
        select(User).options(selectinload(User.ps_tenant)).order_by(User.created_at)
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
    )
    db.add(user)
    await db.commit()
    await db.refresh(user, ["ps_tenant"])
    await _log_audit(db, admin.id, admin.email, "user_created", f"email={user.email} role={user.role}")
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

    await db.commit()
    await db.refresh(user, ["ps_tenant"])
    changed = [k for k in ("email","role","is_active","ps_enabled","ps_tenant_id") if getattr(body,k,None) is not None]
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
        select(User.email, func.count(Message.id).label("count"))
        .join(Message, Message.user_id == User.id)
        .where(Message.role == "user")
        .group_by(User.email)
        .order_by(desc("count"))
        .limit(10)
    )
    top_users = [{"email": r[0], "message_count": r[1]} for r in top_users_result.all()]

    # Per-user message counts today
    user_counts_result = await db.execute(
        select(User.email, func.count(Message.id).label("count"))
        .join(Message, Message.user_id == User.id)
        .where(Message.created_at >= today_start, Message.role == "user")
        .group_by(User.email)
        .order_by(desc("count"))
    )
    user_counts = [{"email": r[0], "count": r[1]} for r in user_counts_result.all()]

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
        select(Message).where(Message.user_id == user_id).order_by(desc(Message.id)).limit(10)
    )
    recent = [
        {"role": m.role, "content_preview": (m.content or "")[:120],
         "model": m.model, "ps_action": m.ps_action, "created_at": m.created_at.isoformat()}
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

    # skip_ps is privileged: non-admin requests are explicitly rejected.
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
        if ps_gw_gemini:
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

        if ps_gw_anthropic:
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
            if not ps_client:
                stream_kwargs["max_tokens"] = 150
            if llm is litellm_client:
                stream_kwargs["stream_options"] = {"include_usage": True}
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
        await _log_msg(db, session_id, current_user.id, "assistant", reply, model,
                       ps_scanned=ps_active, ps_action=final_action,
                       ps_violations=ps_violations, response_ms=resp_ms)

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
        .join(User, User.id == Message.user_id)
        .order_by(desc(Message.created_at))
        .limit(min(limit, 500))
    )
    chat_rows = [
        {
            "id": f"msg-{msg.id}",
            "entry_type": "chat",
            "user_email": email,
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
            "model": None,
            "ps_scanned": False,
            "ps_action": None,
            "created_at": ev.created_at.isoformat(),
        }
        for ev in audit_result.scalars().all()
    ]

    combined = sorted(chat_rows + audit_rows, key=lambda r: r["created_at"], reverse=True)
    return combined[:min(limit, 500)]


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


def _build_ps_api_client(user: User) -> Optional[PromptSecurityClient]:
    if not (user.ps_enabled and user.ps_tenant and user.ps_api_key_enc):
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
    user_id: int,
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
    user_id: int,
    role: str,
    content: str,
    model: Optional[str],
    ps_scanned: bool = False,
    ps_blocked: bool = False,
    ps_action: str = "pass",
    ps_violations: Optional[list] = None,
    response_ms: Optional[int] = None,
):
    msg = Message(
        session_id=session_id,
        user_id=user_id,
        role=role,
        content=content,
        model=model,
        ps_scanned=ps_scanned,
        ps_action=ps_action if not ps_blocked else "block",
        ps_violations=ps_violations or [],
        response_ms=response_ms,
    )
    db.add(msg)
    await db.commit()
