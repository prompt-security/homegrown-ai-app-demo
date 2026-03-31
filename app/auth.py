import os
import secrets
from hashlib import sha256
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database import get_db
from models import APIKey, User

SECRET_KEY  = os.getenv("SECRET_KEY", "dev_secret_change_me")
ALGORITHM   = "HS256"
TOKEN_TTL_H = 24

pwd_ctx  = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer   = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return pwd_ctx.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_ctx.verify(plain, hashed)


def create_access_token(data: dict, expires_h: int = TOKEN_TTL_H) -> str:
    payload = data.copy()
    payload["exp"] = datetime.now(timezone.utc) + timedelta(hours=expires_h)
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_api_key() -> str:
    return f"hg_live_{secrets.token_urlsafe(24)}"


def hash_api_key(raw_key: str) -> str:
    return sha256(raw_key.encode()).hexdigest()


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    exc = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    if not credentials:
        raise exc
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: int = int(payload.get("sub", 0))
    except (JWTError, ValueError):
        raise exc

    result = await db.execute(
        select(User)
        .where(User.id == user_id, User.is_active == True)
        .options(selectinload(User.ps_tenant))
    )
    user = result.scalar_one_or_none()
    if not user:
        raise exc
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user


async def get_current_api_key(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> tuple[APIKey, User]:
    auth_header = request.headers.get("authorization", "").strip()
    exc = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    if not auth_header.lower().startswith("bearer "):
        raise exc

    raw_key = auth_header[7:].strip()
    if not raw_key:
        raise exc

    result = await db.execute(
        select(APIKey, User)
        .join(User, User.id == APIKey.user_id)
        .where(
            APIKey.key_hash == hash_api_key(raw_key),
            APIKey.is_active == True,
            User.is_active == True,
        )
        .options(selectinload(User.ps_tenant))
    )
    row = result.one_or_none()
    if not row:
        raise exc

    api_key, user = row
    api_key.last_used_at = datetime.now(timezone.utc)
    await db.commit()
    return api_key, user
