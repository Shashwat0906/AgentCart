"""Input sanitisation, prompt-injection detection and output grounding checks.

Defence in depth: detection here only *flags* suspicious text so we can tell the user.
The real protection is structural — the LLM never decides prices, stock, ranking or
cart mutations, and every state-changing action requires explicit user approval.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

MAX_MESSAGE_CHARS = 2000

_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("override_instructions", re.compile(r"\b(ignore|disregard|forget|override)\b.{0,40}\b(instructions?|rules?|prompts?|guidelines?|polic(y|ies))\b", re.I)),
    ("role_hijack", re.compile(r"\b(you are now|act as|pretend to be|developer mode|jailbreak|dan mode)\b", re.I)),
    ("system_prompt", re.compile(r"\b(system prompt|system message|hidden prompt|reveal (your|the) (prompt|instructions))\b", re.I)),
    ("price_manipulation", re.compile(r"\b(set|change|make|update|override)\b.{0,30}\bprice\b|\bprice\b.{0,20}\b(to|=)\s*(₹|rs\.?|inr)?\s*0\b|\b100\s*%\s*(discount|off)\b|\b(free of cost|for free)\b", re.I)),
    ("approval_bypass", re.compile(r"\b(without|skip|bypass|no need (for|to))\b.{0,30}\b(approv\w*|confirm\w*|asking|permission|consent)\b", re.I)),
    ("auto_checkout", re.compile(r"\b(checkout|place (the |an )?order|pay)\b.{0,20}\b(now|immediately|automatically|for me)\b", re.I)),
    ("privilege", re.compile(r"\b(admin|root|sudo|superuser)\b.{0,20}\b(mode|access|privileges?|rights)\b", re.I)),
    ("other_user", re.compile(r"\b(another|other|different) (user|account|customer)'?s?\b.{0,30}\b(cart|order|data|preferences?)\b", re.I)),
]

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁠-⁤﻿]")


def sanitize_user_text(text: str) -> str:
    """Normalise unicode, drop control / bidi / zero-width characters, cap length."""
    text = unicodedata.normalize("NFKC", text or "")
    text = _CONTROL_CHARS.sub("", text)
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text[:MAX_MESSAGE_CHARS]


def detect_injection(text: str) -> list[str]:
    return [name for name, pattern in _INJECTION_PATTERNS if pattern.search(text or "")]


SECURITY_NOTICE = (
    "Part of your message looked like an instruction to change how I work (for example skipping "
    "approval, changing prices or ignoring rules). I treated it as plain text: prices and stock always "
    "come from the catalogue, and every cart change needs your explicit approval."
)

# --------------------------------------------------------------------------- grounding

_RUPEE_AMOUNT = re.compile(r"(?:₹|\brs\.?|\binr)\s*(\d[\d,]*)", re.I)
_RATING = re.compile(r"(\d(?:\.\d)?)\s*(?:★|stars?|/\s*5)", re.I)
_URL = re.compile(r"https?://\S+|www\.\S+", re.I)


@dataclass(frozen=True)
class GroundingFacts:
    product_names: set[str]
    other_catalog_names: set[str]
    allowed_amounts: set[int]
    allowed_ratings: set[float]


def build_facts(
    recommended: Iterable[tuple[str, int, float]],
    catalog_names: Iterable[str],
    extra_amounts: Iterable[int] = (),
) -> GroundingFacts:
    rec = list(recommended)
    names = {n for n, _, _ in rec}
    amounts = {p for _, p, _ in rec} | {int(a) for a in extra_amounts}
    # Differences (e.g. "₹7,010 under budget") are also grounded facts.
    for a in list(amounts):
        for b in list(amounts):
            if a > b:
                amounts.add(a - b)
    return GroundingFacts(
        product_names=names,
        other_catalog_names={n for n in catalog_names if n not in names},
        allowed_amounts=amounts,
        allowed_ratings={round(r, 1) for _, _, r in rec},
    )


def check_grounding(text: str, facts: GroundingFacts) -> list[str]:
    """Return a list of problems; empty means every checkable claim is grounded."""
    problems: list[str] = []
    if _URL.search(text):
        problems.append("contains a URL")
    for m in _RUPEE_AMOUNT.finditer(text):
        amount = int(m.group(1).replace(",", "") or 0)
        if amount not in facts.allowed_amounts:
            problems.append(f"unsupported price ₹{amount:,}")
    for m in _RATING.finditer(text):
        try:
            rating = round(float(m.group(1)), 1)
        except ValueError:
            continue
        if rating <= 5 and rating not in facts.allowed_ratings:
            problems.append(f"unsupported rating {rating}")
    lowered = text.lower()
    for name in facts.other_catalog_names:
        if len(name) > 8 and name.lower() in lowered:
            problems.append(f"mentions non-recommended product '{name}'")
    return problems
