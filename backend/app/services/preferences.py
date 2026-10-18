"""Persistent, user-approved shopping preferences."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.models import Preference
from app.services.specs import CATEGORY_SPECS

PREFERENCE_KINDS = ("preferred_brand", "avoided_brand", "budget_max", "use_case", "note")
MAX_PREFERENCES = 50

_SENSITIVE = [
    ("an email address", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("a phone number", re.compile(r"(?:\+?\d[\s-]?){10,}")),
    ("a card or account number", re.compile(r"\b(?:\d[ -]?){12,19}\b")),
    ("an Aadhaar/PAN-like identifier", re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b|\b[A-Z]{5}\d{4}[A-Z]\b", re.I)),
    ("a password or secret", re.compile(r"\b(password|passcode|pin|otp|cvv|secret|api[_ ]?key)\b", re.I)),
    ("a street address", re.compile(r"\b(flat|house|plot|street|road|lane|sector|pincode|pin code)\b.{0,30}\d", re.I)),
    (
        "health or financial details",
        re.compile(r"\b(diagnos\w*|disease|medical|salary|income|loan|debt|religion|caste)\b", re.I),
    ),
]


def sensitive_reason(value: str) -> str | None:
    for reason, rx in _SENSITIVE:
        if rx.search(value):
            return reason
    return None


def normalise(kind: str, value: str, category: str | None) -> tuple[str, str, str | None]:
    if kind not in PREFERENCE_KINDS:
        raise ValidationFailed(f"Unsupported preference kind '{kind}'")
    value = re.sub(r"\s+", " ", value).strip()
    if not value or len(value) > 200:
        raise ValidationFailed("Preference value must be 1–200 characters")
    if category is not None and category not in CATEGORY_SPECS:
        raise ValidationFailed(f"Unknown category '{category}'")
    reason = sensitive_reason(value)
    if reason:
        raise ValidationFailed(f"For your privacy, preferences cannot contain {reason}.")
    if kind == "budget_max":
        digits = value.replace(",", "").replace("₹", "").strip()
        if not digits.isdigit() or not 100 <= int(digits) <= 10_000_000:
            raise ValidationFailed("Budget must be a whole number of rupees between 100 and 1,00,00,000")
        if category is None:
            raise ValidationFailed("A budget preference needs a category")
        value = digits
    elif kind in {"preferred_brand", "avoided_brand"} and len(value) > 40:
        raise ValidationFailed("Brand name is too long")
    return kind, value, category


def list_preferences(db: Session, user_id: int) -> list[Preference]:
    return list(db.scalars(select(Preference).where(Preference.user_id == user_id).order_by(Preference.id)))


def as_dicts(prefs: list[Preference]) -> list[dict[str, Any]]:
    return [{"kind": p.kind, "value": p.value, "category": p.category} for p in prefs]


def create_preference(
    db: Session, user_id: int, kind: str, value: str, category: str | None, source: str = "user"
) -> Preference:
    kind, value, category = normalise(kind, value, category)
    if len(list_preferences(db, user_id)) >= MAX_PREFERENCES:
        raise ValidationFailed(f"You can save at most {MAX_PREFERENCES} preferences")
    # A brand cannot be both preferred and avoided.
    opposite = {"preferred_brand": "avoided_brand", "avoided_brand": "preferred_brand"}.get(kind)
    if opposite:
        for p in list_preferences(db, user_id):
            if p.kind == opposite and p.value.lower() == value.lower():
                db.delete(p)
    if kind == "budget_max":
        for p in list_preferences(db, user_id):
            if p.kind == "budget_max" and p.category == category:
                p.value = value
                p.source = source
                db.commit()
                return p
    # Checked in code as well as by the unique constraint: SQL treats NULL categories as
    # distinct, so the constraint alone would allow duplicate category-less preferences.
    for p in list_preferences(db, user_id):
        if p.kind == kind and p.category == category and p.value.lower() == value.lower():
            raise ConflictError("That preference is already saved")
    pref = Preference(user_id=user_id, kind=kind, value=value, category=category, source=source)
    db.add(pref)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError("That preference is already saved") from exc
    return pref


def _owned(db: Session, user_id: int, pref_id: int) -> Preference:
    pref = db.get(Preference, pref_id)
    if pref is None or pref.user_id != user_id:
        raise NotFoundError("Preference not found")
    return pref


def update_preference(db: Session, user_id: int, pref_id: int, value: str | None, category: str | None) -> Preference:
    pref = _owned(db, user_id, pref_id)
    _, new_value, new_cat = normalise(
        pref.kind, value if value is not None else pref.value, category if category is not None else pref.category
    )
    for p in list_preferences(db, user_id):
        if p.id != pref.id and p.kind == pref.kind and p.category == new_cat and p.value.lower() == new_value.lower():
            raise ConflictError("That preference is already saved")
    pref.value, pref.category = new_value, new_cat
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError("That preference is already saved") from exc
    return pref


def delete_preference(db: Session, user_id: int, pref_id: int) -> None:
    db.delete(_owned(db, user_id, pref_id))
    db.commit()
