import hashlib
import secrets
from datetime import timedelta

import jwt
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_secret_key, get_settings
from .db import get_db, utcnow
from .models import Device, User

_hasher = PasswordHash.recommended()
_bearer = HTTPBearer(auto_error=False)


def hash_password(p: str) -> str:
    return _hasher.hash(p)


def verify_password(p: str, h: str) -> bool:
    return _hasher.verify(p, h)


def new_device_key() -> str:
    return "dk_" + secrets.token_urlsafe(32)


def hash_device_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_token(user: User) -> str:
    s = get_settings()
    exp = utcnow() + timedelta(minutes=s.access_token_minutes)
    return jwt.encode({"sub": user.id, "role": user.role, "email": user.email, "exp": exp}, get_secret_key(s), algorithm="HS256")


def current_admin(
    cred: HTTPAuthorizationCredentials | None = Depends(_bearer), db: Session = Depends(get_db)
) -> User:
    err = HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in required", headers={"WWW-Authenticate": "Bearer"})
    if cred is None:
        raise err
    try:
        payload = jwt.decode(cred.credentials, get_secret_key(get_settings()), algorithms=["HS256"])
    except jwt.PyJWTError:
        raise err
    user = db.get(User, payload.get("sub"))
    if user is None or user.role != "ADMIN":
        raise err
    return user


def current_device(x_device_key: str | None = Header(default=None), db: Session = Depends(get_db)) -> Device:
    if not x_device_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Device key required")
    dev = db.scalar(select(Device).where(Device.key_hash == hash_device_key(x_device_key)))
    if dev is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid device key")
    return dev
