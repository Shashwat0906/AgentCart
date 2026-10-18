from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AuthError, ConflictError
from app.core.security import hash_password, verify_password
from app.models import User


def register_user(db: Session, email: str, password: str, display_name: str) -> User:
    email = email.strip().lower()
    if db.scalar(select(User).where(func.lower(User.email) == email)):
        raise ConflictError("An account with this email already exists")
    user = User(email=email, password_hash=hash_password(password), display_name=display_name.strip())
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError("An account with this email already exists") from exc
    return user


def authenticate(db: Session, email: str, password: str) -> User:
    user = db.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))
    # verify_password runs against a dummy hash when the user is missing (timing equalisation)
    if not verify_password(password, user.password_hash if user else None) or user is None:
        raise AuthError("Incorrect email or password")
    return user
