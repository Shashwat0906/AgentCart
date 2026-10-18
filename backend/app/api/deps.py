from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import AuthError, RateLimited
from app.core.rate_limit import limiter
from app.core.security import decode_access_token
from app.db.session import get_db
from app.models import User
from app.providers import LLMProvider, build_provider

_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if creds is None or creds.scheme.lower() != "bearer" or not creds.credentials:
        raise AuthError("Authentication required")
    user_id = decode_access_token(creds.credentials)
    user = db.get(User, user_id)
    if user is None:
        raise AuthError("Invalid authentication token")
    request.state.user_id = user.id
    return user


def user_rate_limit(bucket: str, per_minute: Callable[[Settings], int]) -> Callable[..., None]:
    """Per-user limiter (runs after authentication)."""

    def dependency(user: User = Depends(get_current_user), settings: Settings = Depends(get_settings)) -> None:
        if not settings.rate_limit_enabled:
            return
        retry = limiter.hit(f"{bucket}:user:{user.id}", per_minute(settings))
        if retry is not None:
            raise RateLimited("Too many requests. Please slow down.", details={"retry_after_seconds": round(retry, 1)})

    return dependency


@lru_cache
def _provider() -> LLMProvider:
    return build_provider(get_settings())


def get_provider() -> LLMProvider:
    return _provider()


chat_limit = user_rate_limit("chat", lambda s: s.rate_limit_chat_per_minute)
write_limit = user_rate_limit("write", lambda s: s.rate_limit_write_per_minute)
