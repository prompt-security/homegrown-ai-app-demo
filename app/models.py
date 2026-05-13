from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class PSTenant(Base):
    __tablename__ = "ps_tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    base_url: Mapped[str] = mapped_column(String(255))
    gateway_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    users: Mapped[list["User"]] = relationship("User", back_populates="ps_tenant")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(20), default="se")  # admin | se
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    daily_message_limit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    allowed_models: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)  # null = all
    ps_tenant_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("ps_tenants.id", ondelete="SET NULL"), nullable=True
    )
    ps_api_key_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # Fernet-encrypted
    ps_mode: Mapped[str] = mapped_column(String(10), default="api")              # api | gateway
    ps_enabled: Mapped[bool] = mapped_column(Boolean, default=True)              # admin-toggleable on/off
    llm_api_keys_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # encrypted JSON {provider: key}
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    ps_tenant: Mapped[Optional["PSTenant"]] = relationship("PSTenant", back_populates="users")
    sessions: Mapped[list["ChatSession"]] = relationship("ChatSession", back_populates="user")
    messages: Mapped[list["Message"]] = relationship("Message", back_populates="user")
    api_keys: Mapped[list["APIKey"]] = relationship("APIKey", back_populates="user")


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    guest_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255), default="New Conversation")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    user: Mapped[Optional[User]] = relationship("User", back_populates="sessions")
    messages: Mapped[list["Message"]] = relationship(
        "Message", back_populates="session", order_by="Message.id",
        cascade="all, delete-orphan",
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("chat_sessions.id", ondelete="CASCADE"))
    user_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    guest_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(20))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    ps_scanned: Mapped[bool] = mapped_column(Boolean, default=False)
    ps_action: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # pass | modify | block
    ps_violations: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    response_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    session: Mapped[ChatSession] = relationship("ChatSession", back_populates="messages")
    user: Mapped[Optional[User]] = relationship("User", back_populates="messages")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    user_email: Mapped[str] = mapped_column(String(255))
    event_type: Mapped[str] = mapped_column(String(60))   # ps_config_changed | llm_key_added | user_created | etc.
    detail: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class DemoScenario(Base):
    __tablename__ = "demo_scenarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(150))
    category: Mapped[str] = mapped_column(String(60))
    severity: Mapped[str] = mapped_column(String(10), default="MEDIUM")
    prompt: Mapped[str] = mapped_column(Text)
    expected_action: Mapped[str] = mapped_column(String(20), default="block")
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attacker_goal: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    why_caught: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    talking_point: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    entities: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class APIKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    key_prefix: Mapped[str] = mapped_column(String(24), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    user: Mapped[User] = relationship("User", back_populates="api_keys")
