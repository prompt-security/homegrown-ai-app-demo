import base64
import io
import json
import logging
import os
import time
import uuid
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

from auth import create_access_token, get_current_user, hash_password, require_admin, verify_password
from crypto import decrypt, encrypt
from database import AsyncSessionLocal, Base, engine, get_db
from models import AuditEvent, ChatSession, Message, PSTenant, User
from prompt_security import PromptSecurityClient
from schemas import (
    ChatMessage, ChatRequest, ChatResponse,
    LLMKeysUpdate, LoginRequest, MessageOut, PSConfigUpdate, PSTenantCreate, PSTenantOut, PSTenantUpdate,
    SessionOut, TokenResponse, UserCreate, UserOut, UserStats, UserUpdate,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(name)s  %(message)s")
logger = logging.getLogger("main")

# ── Configuration ─────────────────────────────────────────────────────────────
LITELLM_BASE_URL   = os.getenv("LITELLM_BASE_URL", "http://litellm:4000")
LITELLM_MASTER_KEY = os.getenv("LITELLM_MASTER_KEY", "")
ADMIN_EMAIL        = os.getenv("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD     = os.getenv("ADMIN_PASSWORD", "admin")
DEFAULT_DAILY_LIMIT = int(os.getenv("DEFAULT_DAILY_LIMIT", "50")) or None

# Shared LLM keys (fallback when user has no per-provider key)
_SHARED_LLM_KEYS = {
    "openai":     os.getenv("OPENAI_API_KEY", ""),
    "anthropic":  os.getenv("ANTHROPIC_API_KEY", ""),
    "google":     os.getenv("GOOGLE_API_KEY", ""),
    "openrouter": os.getenv("OPENROUTER_API_KEY", ""),
}

# ── File upload limits ────────────────────────────────────────────────────────
# Set MAX_FILE_SIZE_MB in .env to restrict upload size.
MAX_FILE_SIZE_MB    = int(os.getenv("MAX_FILE_SIZE_MB", "10"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
ALLOWED_TEXT_TYPES  = {"text/plain", "text/markdown", "text/csv", "application/json"}
ALLOWED_PDF_TYPE    = "application/pdf"

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
    return "openrouter"


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
    {"id": "claude-3-5-sonnet-20241022"},
    {"id": "claude-3-5-haiku-20241022"},
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


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
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
    return {"models": available, "fallback": not bool(live)}


@app.post("/admin/refresh-models")
async def admin_refresh_models(admin: User = Depends(require_admin)):
    updated = await refresh_model_cache()
    return {"models_loaded": len(updated), "fallback": not bool(updated)}


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


# ── File upload ───────────────────────────────────────────────────────────────
@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    data = await file.read()
    if len(data) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large. Max {MAX_FILE_SIZE_MB} MB.")

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
    session_id = request.session_id or str(uuid.uuid4())
    session = await db.scalar(select(ChatSession).where(ChatSession.id == session_id))
    if not session:
        title = last_user_msg[:60] + ("…" if len(last_user_msg) > 60 else "")
        session = ChatSession(id=session_id, user_id=current_user.id, title=title)
        db.add(session)
        await db.commit()

    # ── Per-user PS client / mode ─────────────────────────────────────────────
    ps_mode        = current_user.ps_mode or "api"
    ps_client: Optional[PromptSecurityClient] = None
    ps_gw_client: Optional[AsyncOpenAI]       = None  # gateway-mode LLM client
    ps_gw_no_key  = False  # set True when gateway mode lacks an LLM key

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
                    logger.info("PS gateway init for %s → %s (llm_key set: %s)",
                                current_user.email, current_user.ps_tenant.gateway_url, bool(llm_key))
                    if llm_key:
                        ps_gw_client = AsyncOpenAI(
                            api_key=llm_key,
                            base_url=current_user.ps_tenant.gateway_url,
                            default_headers={"ps-app-id": ps_app_id},
                        )
                    else:
                        ps_gw_no_key = True
                        logger.warning("Gateway mode: no LLM key for %s, will error in stream", current_user.email)
            except Exception as e:
                logger.warning("Could not init PS client for %s: %s", current_user.email, e)

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

    async def generate():
        reply = ""
        prompt_action = "pass"
        t0 = time.monotonic()

        msgs = list(request.messages)
        payload = [{"role": "system", "content": system_prompt}] + [
            {"role": m.role, "content": _build_content(m)} for m in msgs
        ]

        # ── Gateway mode: route through PS proxy, skip explicit scanning ───────
        if ps_gw_no_key:
            yield f"data: {json.dumps({'type': 'error', 'detail': 'Gateway mode requires an LLM API key. Add your OpenRouter key in ⚙ Settings → LLM API Keys, or set OPENROUTER_API_KEY in .env.'})}\n\n"
            return
        if ps_gw_client:
            try:
                stream = await ps_gw_client.chat.completions.create(
                    model=model, messages=payload, stream=True
                )
                async for chunk in stream:
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
            await _log_msg(db, session_id, current_user.id, "assistant", reply, model,
                           ps_scanned=True, ps_action="pass", response_ms=resp_ms)
            today_used = used_today + 1
            yield f"data: {json.dumps({'type': 'done', 'model': model, 'session_id': session_id, 'ps_scanned': True, 'ps_action': 'gateway', 'ps_violations': [], 'messages_today': today_used, 'daily_limit': current_user.daily_message_limit})}\n\n"
            return

        # ── API mode: explicit PS scan + LiteLLM/per-user key ─────────────────
        if ps_client:
            try:
                ps_result = await ps_client.protect_prompt(
                    user_prompt=last_user_msg,
                    system_prompt=system_prompt,
                    user=current_user.email,
                )
                if not ps_result.allowed:
                    await _log_msg(db, session_id, current_user.id, "assistant", "", model,
                                   ps_scanned=True, ps_blocked=True, ps_action="block",
                                   ps_violations=ps_result.violations)
                    yield f"data: {json.dumps({'type': 'blocked', 'action': 'block', 'violations': ps_result.violations})}\n\n"
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
            stream = await llm.chat.completions.create(
                model=effective_model, messages=payload, stream=True
            )
            async for chunk in stream:
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
        ps_violations: list = []
        if ps_client and reply:
            try:
                ps_resp = await ps_client.protect_response(
                    response_text=reply,
                    user_prompt=last_user_msg,
                    system_prompt=system_prompt,
                    user=current_user.email,
                )
                ps_violations = ps_resp.violations
                if not ps_resp.allowed:
                    await _log_msg(db, session_id, current_user.id, "assistant", reply, model,
                                   ps_scanned=True, ps_blocked=True, ps_action="block",
                                   ps_violations=ps_violations, response_ms=resp_ms)
                    yield f"data: {json.dumps({'type': 'revoke', 'action': 'block', 'violations': ps_violations})}\n\n"
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

        await _log_msg(db, session_id, current_user.id, "assistant", reply, model,
                       ps_scanned=bool(ps_client), ps_action=final_action,
                       ps_violations=ps_violations, response_ms=resp_ms)

        today_used = used_today + 1
        yield f"data: {json.dumps({'type': 'done', 'model': model, 'session_id': session_id, 'ps_scanned': bool(ps_client), 'ps_action': final_action, 'ps_violations': ps_violations, 'messages_today': today_used, 'daily_limit': current_user.daily_message_limit})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
        llm_keys_configured=llm_providers,
        created_at=user.created_at,
    )


def _build_content(m: ChatMessage):
    if m.image_url:
        parts = [{"type": "image_url", "image_url": {"url": m.image_url}}]
        if m.content:
            parts.append({"type": "text", "text": m.content})
        return parts
    return m.content


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
