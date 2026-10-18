"""Password hashing (Argon2id) and JWT access tokens."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import get_settings
from app.core.errors import AuthError

_hasher = PasswordHasher()  # argon2id with library-recommended parameters
# Used to equalise timing when a login email does not exist.
_DUMMY_HASH = _hasher.hash("agentcart-timing-equaliser")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def create_access_token(user_id: int) -> tuple[str, int]:
    settings = get_settings()
    now = datetime.now(UTC)
    expires = timedelta(minutes=settings.jwt_expire_minutes)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "nbf": now,
        "exp": now + expires,
        "jti": uuid.uuid4().hex,
        "typ": "access",
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, int(expires.total_seconds())


def decode_access_token(token: str) -> int:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub", "iat"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Session expired. Please sign in again.") from exc
    except jwt.PyJWTError as exc:
        raise AuthError("Invalid authentication token") from exc
    if payload.get("typ") != "access":
        raise AuthError("Invalid authentication token")
    try:
        return int(payload["sub"])
    except (TypeError, ValueError) as exc:
        raise AuthError("Invalid authentication token") from exc
