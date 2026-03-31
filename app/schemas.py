from datetime import datetime
from typing import Optional
from pydantic import BaseModel, EmailStr


# ── Auth ──────────────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    email: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: "UserOut"


# ── PS Tenants ────────────────────────────────────────────────────────────────
class PSTenantCreate(BaseModel):
    name: str
    base_url: str
    gateway_url: Optional[str] = None

class PSTenantUpdate(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    gateway_url: Optional[str] = None

class PSTenantOut(BaseModel):
    id: int
    name: str
    base_url: str
    gateway_url: Optional[str]
    created_at: datetime
    model_config = {"from_attributes": True}


# ── Users ─────────────────────────────────────────────────────────────────────
class UserCreate(BaseModel):
    email: str
    password: str
    role: str = "se"
    daily_message_limit: Optional[int] = None
    allowed_models: Optional[list[str]] = None
    ps_tenant_id: Optional[int] = None

class UserUpdate(BaseModel):
    email: Optional[str] = None
    password: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    daily_message_limit: Optional[int] = None
    allowed_models: Optional[list[str]] = None
    ps_tenant_id: Optional[int] = None
    ps_enabled: Optional[bool] = None

class UserOut(BaseModel):
    id: int
    email: str
    role: str
    is_active: bool
    daily_message_limit: Optional[int]
    allowed_models: Optional[list[str]]
    ps_tenant_id: Optional[int]
    ps_tenant: Optional[PSTenantOut]
    ps_configured: bool = False
    ps_mode: str = "api"
    ps_enabled: bool = True
    llm_keys_configured: list[str] = []  # list of providers with keys set, e.g. ["openai","openrouter"]
    created_at: datetime
    model_config = {"from_attributes": True}


# ── User PS settings (self-service) ───────────────────────────────────────────
class PSConfigUpdate(BaseModel):
    ps_tenant_id: Optional[int] = None
    ps_api_key: Optional[str] = None    # plaintext — encrypted server-side
    ps_mode: Optional[str] = None       # "api" | "gateway"
    ps_enabled: Optional[bool] = None


# ── User LLM key overrides (self-service) ────────────────────────────────────
class LLMKeysUpdate(BaseModel):
    openai: Optional[str] = None        # empty string = clear the key
    anthropic: Optional[str] = None
    google: Optional[str] = None
    openrouter: Optional[str] = None


# ── Chat ──────────────────────────────────────────────────────────────────────
class ChatMessage(BaseModel):
    role: str
    content: str
    image_url: Optional[str] = None     # base64 data URL for vision models

class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    session_id: Optional[str] = None


class TokenEstimateResponse(BaseModel):
    estimated_prompt_tokens: int
    model: str

class ChatResponse(BaseModel):
    reply: str
    model: str
    ps_scanned: bool = False
    ps_action: str = "pass"
    ps_violations: list = []


# ── Sessions ──────────────────────────────────────────────────────────────────
class SessionOut(BaseModel):
    id: str
    title: str
    created_at: datetime
    model_config = {"from_attributes": True}

class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    model: Optional[str]
    ps_scanned: bool
    ps_action: Optional[str]
    ps_violations: Optional[list]
    created_at: datetime
    model_config = {"from_attributes": True}


# ── Stats ─────────────────────────────────────────────────────────────────────
class UserStats(BaseModel):
    messages_today: int
    daily_limit: Optional[int]
