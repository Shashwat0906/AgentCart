"""Deterministic natural-language → ShoppingIntent parser.

This is the default extractor (the "mock" provider path), so the whole agent works without
any LLM. An optional LLM can *add* information on top of it, but the result is always
re-validated against the spec registry.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Literal

from app.agents.intent import AlternativesPolicy, Constraint, Op, PreferenceProposal, ShoppingIntent, SoftPreference
from app.services.specs import format_inr

Direction = Literal["max", "min", "true"]

# --------------------------------------------------------------------------- vocab

CATEGORY_SYNONYMS: dict[str, list[str]] = {
    "laptops": ["laptop", "laptops", "notebook", "notebooks", "macbook", "macbooks", "ultrabook", "ultrabooks"],
    "smartphones": [
        "smartphone",
        "smartphones",
        "phone",
        "phones",
        "mobile",
        "mobiles",
        "iphone",
        "iphones",
        "handset",
        "cellphone",
    ],
    "headphones": [
        "headphones",
        "headphone",
        "earbuds",
        "earbud",
        "earphones",
        "earphone",
        "headset",
        "headsets",
        "tws",
        "airpods",
        "buds",
        "iems",
    ],
    "monitors": ["monitor", "monitors", "external display", "gaming display"],
    "tablets": ["tablet", "tablets", "ipad", "ipads", "tab"],
    "smartwatches": [
        "smartwatch",
        "smartwatches",
        "smart watch",
        "smart watches",
        "watch",
        "watches",
        "fitness tracker",
        "fitness band",
        "running watch",
    ],
    "keyboards": ["keyboard", "keyboards"],
    "mice": ["mouse", "mice"],
    "speakers": ["speaker", "speakers", "bluetooth speaker"],
}

# Words that imply a brand *only* when they also name the requested category.
IMPLIED_BRAND = {
    "macbook": ("Apple", "laptops"),
    "macbooks": ("Apple", "laptops"),
    "iphone": ("Apple", "smartphones"),
    "iphones": ("Apple", "smartphones"),
    "ipad": ("Apple", "tablets"),
    "ipads": ("Apple", "tablets"),
    "airpods": ("Apple", "headphones"),
}

KNOWN_BRANDS = [
    "Lenovo",
    "HP",
    "ASUS",
    "Acer",
    "Apple",
    "Dell",
    "MSI",
    "Samsung",
    "Microsoft",
    "Infinix",
    "Zentrix",
    "OnePlus",
    "Google",
    "Xiaomi",
    "Redmi",
    "Poco",
    "Nothing",
    "iQOO",
    "Motorola",
    "Realme",
    "Vivo",
    "Lava",
    "Sony",
    "Bose",
    "boAt",
    "JBL",
    "Sennheiser",
    "Audio-Technica",
    "Marshall",
    "LG",
    "BenQ",
    "Alienware",
    "Amazfit",
    "Garmin",
    "Noise",
    "Fire-Boltt",
    "Logitech",
    "Keychron",
    "Redragon",
    "HyperX",
    "Cosmic Byte",
    "Razer",
    "Ultimate Ears",
    "Tribit",
]
BRAND_ALIASES = {
    "redmi": "Xiaomi",
    "poco": "Xiaomi",
    "mi": "Xiaomi",
    "moto": "Motorola",
    "boat": "boAt",
    "ue": "Ultimate Ears",
    "audio technica": "Audio-Technica",
    "fire boltt": "Fire-Boltt",
    "fireboltt": "Fire-Boltt",
    "asus rog": "ASUS",
}

BATTERY_ATTR = {
    "laptops": "battery_hours",
    "smartphones": "battery_mah",
    "headphones": "battery_hours",
    "tablets": "battery_hours",
    "smartwatches": "battery_days",
    "speakers": "battery_hours",
}
WEIGHT_ATTR = {"laptops": "weight_kg", "headphones": "weight_g", "mice": "weight_g"}
DISPLAY_ATTR = {
    "laptops": "display_inches",
    "smartphones": "display_inches",
    "tablets": "display_inches",
    "monitors": "size_inches",
}

_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "a pair of": 2, "a couple of": 2}
_ORDINALS = {
    "first": 0,
    "1st": 0,
    "top": 0,
    "best": 0,
    "#1": 0,
    "second": 1,
    "2nd": 1,
    "#2": 1,
    "third": 2,
    "3rd": 2,
    "#3": 2,
    "fourth": 3,
    "4th": 3,
    "fifth": 4,
    "5th": 4,
}

_UNIT_GUARD = (
    r"(?!\s*(?:gb|tb|mah|hz|mp|inch|inches|in\b|\"|w\b|watts?|dpi|h\b|hrs?|hours?|days?|kg|g\b|"
    r"grams?|mm|ms|%|stars?|★|x\b|th\b|nd\b|rd\b|st\b|units?|pieces?|pcs|of them|core))"
)
MONEY = re.compile(
    r"(?P<cur>₹|\brs\.?|\binr\b|\brupees?\b)?\s*"
    # The trailing guard rejects a digit, or a "," / "." that continues the number ("50,0"), but not
    # sentence punctuation: "under ₹50,000, 16GB RAM" and "under ₹50,000." must still parse.
    r"(?<![\d,.])(?P<num>\d{1,3}(?:,\d{2,3})+|\d+(?:\.\d+)?)(?!\d|[,.]\d)\s*"
    r"(?P<suf>k\b|thousand\b|lakhs?\b|lacs?\b|l\b|cr\b|crore\b)?" + _UNIT_GUARD + r"(?:\s*(?:rs\.?|rupees|inr|/-))?",
    re.I,
)
_MAX_CUES = re.compile(
    r"(under|below|less than|lesser than|within|upto|up to|max(imum)?|no more than|not more than|"
    r"cheaper than|at most|budget( of| is| around| about)?|around|about|approx(imately)?|roughly|"
    r"~|spend|for)\s*$",
    re.I,
)
_APPROX_CUES = re.compile(r"(around|about|approx(imately)?|roughly|~)\s*$", re.I)
_MIN_CUES = re.compile(r"(above|over|more than|at least|minimum|min|starting( from| at)?|from)\s*$", re.I)
_MONEY_SIDE = r"(?:₹\s*|rs\.?\s*)?(?<![\d,.])\d[\d,]*(?:\.\d+)?(?!\d|[,.]\d)\s*(?:k\b|lakhs?\b|lacs?\b)?"
_RANGE = re.compile(
    r"(?P<between>between\s+)?(?P<a>" + _MONEY_SIDE + r")\s*(?:-|–|to|and)\s*(?P<b>" + _MONEY_SIDE + r")"
    r"(?!\s*(?:gb|tb|hz|inch|inches|mp|mah|w\b|h\b|hours?|days?|kg|g\b|mm|ms|dpi))",
    re.I,
)

_NEGATION = re.compile(
    r"(\bno\b|\bnot\b|\bavoid\w*|\bexcept\b|\bwithout\b|\bexclud\w*|\banything but\b|\bother than\b|"
    r"\bdon'?t (want|like)\b|\bnever\b|\bhate\b|\bnon\b|\bbesides\b)[\w\s,'-]{0,20}$",
    re.I,
)
_PREFER = re.compile(
    r"(\bprefer\w*|\bideally\b|\blove\b|\blike\b|\bfan of\b|\bfavou?rite\b|\bpreferably\b|\bmaybe\b)[\w\s,'-]{0,20}$",
    re.I,
)
_POSSESSIVE = re.compile(r"\b(for|with|to|pair with|connect to|works with)\s+(my|our|a|an)\s+$", re.I)
_SOFT_CUE = re.compile(
    r"\b(prefer\w*|ideally|nice to have|bonus|if possible|would be nice|optional(ly)?|maybe)\b", re.I
)


def _word_re(phrase: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w-])" + re.escape(phrase) + r"(?![\w-])", re.I)


_CATEGORY_RES = [(cat, _word_re(s)) for cat, syns in CATEGORY_SYNONYMS.items() for s in syns]


def _amount(num: str, suffix: str | None) -> int:
    value = float(num.replace(",", ""))
    s = (suffix or "").lower()
    if s in {"k", "thousand"}:
        value *= 1_000
    elif s.startswith(("lakh", "lac")) or s == "l":
        value *= 100_000
    elif s in {"cr", "crore"}:
        value *= 10_000_000
    return round(value)


def _parse_money_token(token: str) -> int | None:
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(k|lakhs?|lacs?)?", token, re.I)
    return _amount(m.group(1), m.group(2)) if m else None


_QTY_WORDS = {
    "a couple of": 2,
    "a pair of": 2,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
_QTY_NUM = r"(\d{1,4}|" + "|".join(re.escape(w) for w in sorted(_QTY_WORDS, key=len, reverse=True)) + r")"
_QTY_PATTERNS = (
    re.compile(rf"\b(?:add|buy|order|get|take|put|purchase)\s+(?:me\s+)?{_QTY_NUM}(?![\w-])", re.I),  # "add 20 …"
    re.compile(rf"(?<![\w-]){_QTY_NUM}\s*(?:units?|pieces?|pcs|nos)\b", re.I),  # "20 units of …"
    re.compile(rf"\b(?:quantity|qty)\s*(?:of|:|=|to)?\s*{_QTY_NUM}(?![\w-])", re.I),  # "quantity 3"
    re.compile(rf"(?:,|&|\band\b|\bplus\b)\s*{_QTY_NUM}\s+(?=[a-z])", re.I),  # list item: "… and 3 keyboards"
)
# A number followed by one of these is a spec, a price or a duration, not a quantity: "16gb", "4k", "2 years".
_NOT_A_QUANTITY = re.compile(
    r"\s*(?:gb|tb|mb|inch(?:es)?|in\b|\"|hz|mp|mah|w\b|watts?|k\b|%|stars?|★|x\b|mm|ms|dpi|hours?|hrs?|days?|"
    r"weeks?|months?|years?|yrs?|₹|rs\b|rupees?|-)",
    re.I,
)
# Where the product a quantity belongs to stops: a new list item, a clause, or the cart / budget / context words.
_QTY_SCOPE_END = re.compile(
    r"[,.;!?&]|\b(?:and|plus|also|then|to|into|in|for|with|under|below|within|around|between|at|from)\b", re.I
)


def quantity_mentions(text: str) -> list[tuple[int, str | None]]:
    """Each quantity the user asked for, with the category of the product it is attached to (None when no
    product word follows it, e.g. "add 2 to my cart"). Prices, specs and durations are not quantities.

    "add 20 wireless mice under ₹3,000"             -> [(20, "mice")]
    "add 2 mice and 3 keyboards to my cart"          -> [(2, "mice"), (3, "keyboards")]
    "a mouse under ₹3,000, add 10 of the laptop"     -> [(10, "laptops")]
    "add a 16gb laptop" / "for my 2 kids"            -> []
    """
    low = text.lower()
    found: dict[int, tuple[int, str | None]] = {}
    for rx in _QTY_PATTERNS:
        for m in rx.finditer(low):
            start, end = m.start(1), m.end(1)
            if start in found or _NOT_A_QUANTITY.match(low, end) or low[max(0, start - 1) : start] in ("₹", "#"):
                continue
            token = m.group(1)
            n = int(token) if token.isdigit() else _QTY_WORDS[token]
            scope = low[end : end + 60]
            stop = _QTY_SCOPE_END.search(scope)
            scope = scope[: stop.start()] if stop else scope
            hits = sorted((h.start(), cat) for cat, crx in _CATEGORY_RES if (h := crx.search(scope)))
            category = hits[0][1] if hits else None
            if rx is _QTY_PATTERNS[3] and category is None:
                continue  # a bare number in a list ("and 4") is only a quantity when a product follows
            found[start] = (n, category)
    return [found[k] for k in sorted(found)]


_NEGATED_CUE = re.compile(r"\b(nothing|not|no|never)\s+$", re.I)


def strip_budget_phrases(text: str) -> str:
    """`text` without the price expressions `RequestParser` reads as a budget, cue words included, so
    the rest can be used as search keywords: "laptop under ₹60,000" -> "laptop". Uses the same patterns
    and rules as the parser's budget step (ranges, currency or suffix or cue, "4K" is a resolution)."""
    low = text.lower()
    spans: list[tuple[int, int]] = []
    for rng in _RANGE.finditer(low):
        if rng.group("between") or any(re.search(r"₹|rs|k\b|lakh|lac", t) for t in (rng.group("a"), rng.group("b"))):
            spans.append((rng.start(), rng.end()))
    for m in MONEY.finditer(low):
        if any(s <= m.start() < e for s, e in spans):
            continue
        cur, suf = m.group("cur"), m.group("suf")
        before = low[max(0, m.start() - 30) : m.start()]
        cue = _MAX_CUES.search(before) or _MIN_CUES.search(before)
        value = _amount(m.group("num"), suf)
        if not (cur or suf or (cue and value >= 500)) or value < 100:
            continue
        if (suf or "").lower() == "k" and not cur and not cue and m.group("num") in {"2", "4", "5", "8"}:
            continue
        start = m.start() - (len(before) - cue.start()) if cue else m.start()
        if neg := _NEGATED_CUE.search(low[max(0, start - 12) : start]):
            start -= len(neg.group(0))
        spans.append((start, m.end()))
    out = text
    for s, e in sorted(spans, reverse=True):
        out = f"{out[:s]} {out[e:]}"
    return " ".join(out.split())


# --------------------------------------------------------------------------- parser


class RequestParser:
    """Stateless; one instance can be shared."""

    def parse(self, text: str) -> ShoppingIntent:
        t = text.strip()
        low = t.lower()
        intent = ShoppingIntent()

        intent.category = self._category(low)
        self._budget(low, intent)
        self._brands(low, intent)
        self._specs(low, intent)
        self._booleans(low, intent)
        self._soft_preferences(low, intent)
        self._use_cases(low, intent)
        self._rating(low, intent)
        self._cart(low, intent)
        self._sort_hint(low, intent)
        self._alternatives_policy(low, intent)
        self._kind(low, intent)
        self._dedupe(intent)
        return intent

    # ------------------------------------------------------------------ category
    def _category(self, low: str) -> str | None:
        hits: list[tuple[int, str]] = []
        for cat, rx in _CATEGORY_RES:
            m = rx.search(low)
            if m:
                prefix = low[max(0, m.start() - 16) : m.start()]
                # "headphones for my phone" — the phone is context, not the product.
                if _POSSESSIVE.search(prefix):
                    continue
                hits.append((m.start(), cat))
        if not hits:
            return None
        hits.sort()
        return hits[0][1]

    # ------------------------------------------------------------------ budget
    def _budget(self, low: str, intent: ShoppingIntent) -> None:
        for rng in _RANGE.finditer(low):
            a_tok, b_tok = rng.group("a"), rng.group("b")
            marked = any(re.search(r"₹|rs|k\b|lakh|lac", t) for t in (a_tok, b_tok))
            if not marked and not rng.group("between"):
                continue
            a, b = _parse_money_token(a_tok), _parse_money_token(b_tok)
            if not a or not b:
                continue
            # "20-30k": the suffix on the second number applies to the first as well.
            if re.search(r"k\b", b_tok) and not re.search(r"k\b|lakh|lac", a_tok) and a < 1000:
                a *= 1000
            if re.search(r"lakh|lac", b_tok) and not re.search(r"k\b|lakh|lac", a_tok) and a < 100:
                a *= 100_000
            lo, hi = sorted((a, b))
            if hi >= 500:
                intent.budget_min, intent.budget_max = lo, hi
                return
        for m in MONEY.finditer(low):
            cur, suf = m.group("cur"), m.group("suf")
            value = _amount(m.group("num"), suf)
            before = low[max(0, m.start() - 30) : m.start()]
            has_cue = bool(_MAX_CUES.search(before) or _MIN_CUES.search(before))
            if not (cur or suf or (has_cue and value >= 500)):
                continue
            # "4K monitor", "8K TV": a bare 2K/4K/5K/8K without currency or budget cue is a resolution.
            if (suf or "").lower() == "k" and not cur and not has_cue and m.group("num") in {"2", "4", "5", "8"}:
                continue
            if value < 100:
                continue
            if re.search(r"\b(nothing|not|no|never)\s+(over|above|more than|beyond|exceeding)\s*$", before):
                intent.budget_max = value
            elif _MIN_CUES.search(before) and not re.search(r"\b(for|budget)\s*$", before):
                intent.budget_min = value
            elif _APPROX_CUES.search(before):
                intent.budget_max = round(value * 1.1)
                intent.budget_min = round(value * 0.8)
            else:
                intent.budget_max = value
        if intent.budget_min and intent.budget_max and intent.budget_min > intent.budget_max:
            # Leave the contradiction for the validator to surface as a clarifying question.
            pass

    # ------------------------------------------------------------------ brands
    def _brand_mentions(self, low: str) -> Iterable[tuple[str, int]]:
        seen: set[tuple[str, int]] = set()
        for b in KNOWN_BRANDS:
            for m in _word_re(b.lower()).finditer(low):
                after = low[m.end() : m.end() + 14]
                # Brands that are also ordinary English words need extra evidence.
                if b == "Noise" and not re.match(r"\s*(colorfit|smart ?watch|watch|brand|buds)", after):
                    continue
                if b == "Nothing" and not re.match(r"\s*(phone|ear|buds|brand|\()", after):
                    continue
                seen.add((b, m.start()))
        for alias, b in BRAND_ALIASES.items():
            for m in _word_re(alias).finditer(low):
                seen.add((b, m.start()))
        return sorted(seen, key=lambda x: x[1])

    def _brands(self, low: str, intent: ShoppingIntent) -> None:
        include: list[str] = []
        exclude: list[str] = []
        prefer: list[str] = []
        for brand, pos in self._brand_mentions(low):
            before = low[max(0, pos - 28) : pos]
            if _POSSESSIVE.search(before):
                continue
            if _NEGATION.search(before):
                exclude.append(brand)
            elif _PREFER.search(before):
                prefer.append(brand)
            else:
                include.append(brand)
        for word, (brand, cat) in IMPLIED_BRAND.items():
            m = _word_re(word).search(low)
            if m and intent.category == cat and not _POSSESSIVE.search(low[max(0, m.start() - 16) : m.start()]):
                before = low[max(0, m.start() - 28) : m.start()]
                (exclude if _NEGATION.search(before) else include).append(brand)
        include = [b for b in dict.fromkeys(include) if b not in exclude]
        if include:
            intent.constraints.append(
                Constraint(attribute="brand", op="in", value=include, label="Brand: " + " or ".join(include))
            )
        if exclude:
            ex = list(dict.fromkeys(exclude))
            intent.constraints.append(
                Constraint(attribute="brand", op="not_in", value=ex, label="Not " + ", ".join(ex))
            )
        intent.preferred_brands = [b for b in dict.fromkeys(prefer) if b not in exclude]

    # ------------------------------------------------------------------ numeric specs
    def _add(
        self,
        intent: ShoppingIntent,
        attribute: str,
        op: Op,
        value: float | int | bool | str | list[str],
        label: str,
        hard: bool = True,
    ) -> None:
        intent.constraints.append(Constraint(attribute=attribute, op=op, value=value, label=label, hard=hard))

    def _specs(self, low: str, intent: ShoppingIntent) -> None:
        m: re.Match[str] | None
        n: float
        cat = intent.category
        soft = bool(_SOFT_CUE.search(low))
        ram_done = storage_done = False

        for m in re.finditer(
            r"(\d+)\s*gb\s*(?:of\s*)?(?:ram|memory|unified memory)|(?:ram|memory)\s*(?:of\s*|:\s*)?(?:at least\s*|min(?:imum)?\s*)?(\d+)\s*gb",
            low,
        ):
            n = int(m.group(1) or m.group(2))
            self._add(intent, "ram_gb", "gte", n, f"RAM ≥ {n} GB")
            ram_done = True
        for m in re.finditer(
            r"(\d+(?:\.\d+)?)\s*(gb|tb)\s*(?:of\s*)?(?:ssd|storage|rom|internal|nvme|hdd|space)|(?:storage|ssd)\s*(?:of\s*|:\s*)?(?:at least\s*)?(\d+(?:\.\d+)?)\s*(gb|tb)",
            low,
        ):
            n = float(m.group(1) or m.group(3))
            unit = (m.group(2) or m.group(4)).lower()
            gb = int(n * 1024) if unit == "tb" else int(n)
            self._add(
                intent,
                "storage_gb",
                "gte",
                gb,
                f"Storage ≥ {gb if gb < 1024 else gb // 1024} {'GB' if gb < 1024 else 'TB'}",
            )
            storage_done = True
        # Bare "16 GB" with no qualifier: small numbers are RAM, large ones storage.
        if cat in {"laptops", "smartphones", "tablets"}:
            for m in re.finditer(
                r"(?<![\d/])(\d+)\s*(gb|tb)\b(?!\s*(?:ram|memory|ssd|storage|rom|of|internal|nvme|hdd|space|unified|vram))",
                low,
            ):
                n, unit = int(m.group(1)), m.group(2)
                ram_cap = 64 if cat == "laptops" else 24
                if unit == "gb" and n <= ram_cap and not ram_done:
                    self._add(intent, "ram_gb", "gte", n, f"RAM ≥ {n} GB")
                    ram_done = True
                elif not storage_done and (unit == "tb" or n >= 64):
                    gb = n * 1024 if unit == "tb" else n
                    self._add(
                        intent,
                        "storage_gb",
                        "gte",
                        gb,
                        f"Storage ≥ {gb if gb < 1024 else gb // 1024} {'GB' if gb < 1024 else 'TB'}",
                    )
                    storage_done = True

        battery_attr = BATTERY_ATTR.get(cat or "")
        m = re.search(r"(\d{4,5})\s*mah", low)
        if m and cat == "smartphones":
            n = int(m.group(1))
            self._add(intent, "battery_mah", "gte", n, f"Battery ≥ {n} mAh", hard=not soft)
        m = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:hours?|hrs?|h)\b", low)
        if m and battery_attr in {"battery_hours"} and re.search(r"battery|backup|last|playback|playtime", low):
            n = float(m.group(1))
            self._add(intent, "battery_hours", "gte", n, f"Battery ≥ {n:g} h", hard=not soft)
        m = re.search(r"(\d+)\s*\+?\s*days?", low)
        if m and cat == "smartwatches":
            n = int(m.group(1))
            self._add(intent, "battery_days", "gte", n, f"Battery ≥ {n} days", hard=not soft)
        m = re.search(r"(\d{2,3})\s*hz", low)
        if m and cat in {"laptops", "smartphones", "monitors"}:
            n = int(m.group(1))
            self._add(intent, "refresh_hz", "gte", n, f"Refresh rate ≥ {n} Hz", hard=not soft)
        m = re.search(r"(\d{2,3})\s*mp\b", low)
        if m and cat == "smartphones":
            n = int(m.group(1))
            self._add(intent, "main_camera_mp", "gte", n, f"Main camera ≥ {n} MP", hard=not soft)
        m = re.search(r"(\d{2,3})\s*w(?:att)?s?\s*(?:fast\s*|quick\s*)?charg", low)
        if m and cat == "smartphones":
            n = int(m.group(1))
            self._add(intent, "charging_w", "gte", n, f"Charging ≥ {n} W", hard=not soft)
        m = re.search(r"(\d{3,5})\s*dpi", low)
        if m and cat == "mice":
            n = int(m.group(1))
            self._add(intent, "dpi", "gte", n, f"DPI ≥ {n}", hard=not soft)
        m = re.search(
            r"(at least|minimum|min|over|above|under|below|max(?:imum)?|up to)?\s*(\d{1,2}(?:\.\d)?)\s*(?:-\s*)?(?:inch|inches|in\b|\"|”)",
            low,
        )
        if m and cat in DISPLAY_ATTR:
            attr, n, cue = DISPLAY_ATTR[cat], float(m.group(2)), (m.group(1) or "")
            if cue in {"at least", "minimum", "min", "over", "above"}:
                self._add(intent, attr, "gte", n, f'Screen ≥ {n:g}"')
            elif cue in {"under", "below", "max", "maximum", "up to"}:
                self._add(intent, attr, "lte", n, f'Screen ≤ {n:g}"')
            else:
                self._add(intent, attr, "gte", n - 0.6, f'Screen ≈ {n:g}"')
                self._add(intent, attr, "lte", n + 0.6, f'Screen ≈ {n:g}"')
        m = re.search(
            r"(?:under|below|less than|lighter than|max(?:imum)?|within)\s*(\d+(?:\.\d+)?)\s*(kg|g|grams)\b", low
        )
        if m and cat in WEIGHT_ATTR:
            n = float(m.group(1))
            attr = WEIGHT_ATTR[cat]
            if attr == "weight_kg" and m.group(2) != "kg":
                n /= 1000
            if attr == "weight_g" and m.group(2) == "kg":
                n *= 1000
            unit = "kg" if attr == "weight_kg" else "g"
            self._add(intent, attr, "lte", n, f"Weight ≤ {n:g} {unit}")
        if cat == "monitors":
            if re.search(r"\b(4k|uhd|2160p)\b", low):
                self._add(intent, "resolution", "in", ["4K"], "4K resolution")
            elif re.search(r"\b(qhd|1440p|2k|wqhd)\b", low):
                self._add(intent, "resolution", "in", ["QHD", "UWQHD"], "QHD (1440p) resolution")
            elif re.search(r"\b(fhd|1080p|full hd)\b", low):
                self._add(intent, "resolution", "in", ["FHD"], "Full HD resolution")
            if re.search(r"\boled\b", low):
                self._add(intent, "panel", "in", ["OLED"], "OLED panel")
            elif re.search(r"\bips\b", low):
                self._add(intent, "panel", "in", ["IPS"], "IPS panel")

    # ------------------------------------------------------------------ boolean features
    def _booleans(self, low: str, intent: ShoppingIntent) -> None:
        cat = intent.category

        def flag(pattern: str, attribute: str, label: str, value: bool = True, cats: set[str] | None = None) -> None:
            if cats is not None and cat not in cats:
                return
            m = re.search(pattern, low)
            if not m:
                return
            window = low[max(0, m.start() - 40) : m.end() + 25]
            hard = not _SOFT_CUE.search(window)
            negated = re.search(
                r"\b(no|without|don'?t need|not)\s+(\w+\s+){0,2}$", low[max(0, m.start() - 25) : m.start()]
            )
            val = (not value) if negated else value
            final_label = label if val == value else f"No {label.lower()}"
            intent.constraints.append(Constraint(attribute=attribute, op="eq", value=val, label=final_label, hard=hard))

        flag(
            r"upgrad\w*\s+(?:ssd|storage)|(?:ssd|storage)\s+(?:is\s+|that\s+is\s+|should be\s+)?upgrad\w*|expandable storage|extra (?:ssd|m\.?2) slot|m\.?2 slot|storage upgrade",
            "storage_upgradeable",
            "Upgradeable storage",
            cats={"laptops"},
        )
        flag(
            r"upgrad\w*\s+(?:ram|memory)|(?:ram|memory)\s+(?:is\s+|should be\s+)?upgrad\w*|ram upgrade|so-?dimm",
            "ram_upgradeable",
            "Upgradeable RAM",
            cats={"laptops"},
        )
        flag(
            r"dedicated (?:gpu|graphics)|graphics card|discrete (?:gpu|graphics)|\bnvidia\b|\brtx\b|\bgtx\b|\bcuda\b",
            "dedicated_gpu",
            "Dedicated GPU",
            cats={"laptops"},
        )
        flag(
            r"expandable storage|micro ?sd|sd card|memory card",
            "expandable_storage",
            "Expandable storage",
            cats={"smartphones"},
        )
        flag(r"\b5g\b", "has_5g", "5G support", cats={"smartphones"})
        flag(r"noise[- ]?cancel\w*|\banc\b", "anc", "Active noise cancelling", cats={"headphones"})
        flag(
            r"\bwireless\b|\bbluetooth\b|\btws\b|\bcordless\b",
            "wireless",
            "Wireless",
            cats={"headphones", "keyboards", "mice"},
        )
        if (
            cat in {"headphones", "keyboards", "mice"}
            and re.search(r"\bwired\b", low)
            and not re.search(r"\bwireless\b", low)
        ):
            intent.constraints.append(Constraint(attribute="wireless", op="eq", value=False, label="Wired"))
        flag(r"\bgps\b", "gps", "Built-in GPS", cats={"smartwatches"})
        flag(r"stylus|\bpen\b|s ?pen|apple pencil|pencil support", "stylus_support", "Stylus support", cats={"tablets"})
        flag(r"cellular|\blte\b|\bsim\b|\b4g\b|\b5g\b", "cellular", "Cellular connectivity", cats={"tablets"})
        flag(r"\bmechanical\b", "mechanical", "Mechanical switches", cats={"keyboards"})
        flag(r"backlit|backlight\w*|\brgb\b", "backlit", "Backlit keys", cats={"keyboards"})
        flag(
            r"waterproof|water[- ]?resistant|water[- ]?proof|\bip6[78]\b|swim",
            "waterproof",
            "Waterproof",
            cats={"speakers"},
        )
        flag(
            r"waterproof|water[- ]?resistant|water[- ]?proof|\bip6[78]\b|swim",
            "water_resistant",
            "Water resistant",
            cats={"smartwatches"},
        )
        flag(r"usb[- ]?c|type[- ]?c|thunderbolt", "usb_c", "USB-C connectivity", cats={"monitors"})
        flag(r"\bhdr\b", "hdr", "HDR support", cats={"monitors"})
        flag(r"\bergonomic\b", "ergonomic", "Ergonomic shape", cats={"mice"})

        if cat == "headphones":
            if re.search(r"over[- ]?ear", low):
                self._add(intent, "form_factor", "in", ["over-ear"], "Over-ear")
            elif re.search(r"\bin[- ]?ear\b|earbuds?|\btws\b|airpods|\bbuds\b|\biems?\b", low):
                self._add(intent, "form_factor", "in", ["in-ear"], "In-ear / earbuds")
            elif re.search(r"on[- ]?ear", low):
                self._add(intent, "form_factor", "in", ["on-ear"], "On-ear")
        if cat == "keyboards" and re.search(r"\btkl\b|tenkeyless", low):
            self._add(intent, "layout", "in", ["TKL"], "Tenkeyless layout")
        if cat == "laptops":
            if re.search(r"\bmac ?os\b|\bmacbook", low):
                pass  # implied by brand Apple
            elif re.search(r"\bwindows\b", low):
                self._add(intent, "os", "in", ["Windows 11"], "Windows")
        if cat == "smartphones":
            if re.search(r"\bandroid\b", low):
                self._add(intent, "os", "in", ["Android"], "Android")
            elif re.search(r"\bios\b", low):
                self._add(intent, "os", "in", ["iOS"], "iOS")
        if cat == "tablets" and re.search(r"\bandroid\b", low):
            self._add(intent, "os", "in", ["Android"], "Android")
        if cat == "smartwatches":
            if re.search(r"\b(iphone|ios)\b", low):
                self._add(intent, "compatibility", "in", ["iOS", "Android & iOS"], "Works with iPhone")
            elif re.search(r"\bandroid\b", low):
                self._add(intent, "compatibility", "in", ["Android", "Android & iOS"], "Works with Android")

    # ------------------------------------------------------------------ soft prefs
    def _pref(
        self, intent: ShoppingIntent, attribute: str | None, direction: Direction, label: str, weight: float = 1.0
    ) -> None:
        if attribute is None:
            return
        if any(p.attribute == attribute for p in intent.preferences):
            return
        intent.preferences.append(SoftPreference(attribute=attribute, direction=direction, label=label, weight=weight))

    def _soft_preferences(self, low: str, intent: ShoppingIntent) -> None:
        cat = intent.category or ""
        if re.search(
            r"(good|great|long|longer|best|excellent|decent|strong|all[- ]day|long[- ]lasting|big|large|solid|massive)\s+battery|battery life|battery backup|lasts? all day",
            low,
        ):
            self._pref(intent, BATTERY_ATTR.get(cat), "max", "Long battery life", 1.5)
        if re.search(
            r"light ?weight|light-weight|portable|thin and light|easy to carry|\blight\b(?!\s*(gaming|use|usage|work|tasks?|editing|games|bar))",
            low,
        ):
            self._pref(intent, WEIGHT_ATTR.get(cat), "min", "Lightweight", 1.2)
        if cat == "smartphones" and re.search(
            r"(good|great|best|excellent|amazing|nice) camera|photograph\w*|photos|selfies?|camera phone", low
        ):
            self._pref(intent, "main_camera_mp", "max", "Great camera", 1.5)
        if cat == "smartphones" and re.search(r"fast charg\w*|quick charg\w*|rapid charg\w*", low):
            self._pref(intent, "charging_w", "max", "Fast charging")
        if cat in {"laptops", "smartphones", "monitors"} and re.search(
            r"high refresh|smooth (display|screen|scrolling)|high[- ]fps", low
        ):
            self._pref(intent, "refresh_hz", "max", "High refresh rate")
        if re.search(r"(big|large|larger|bigger)\s+(screen|display)", low):
            self._pref(intent, DISPLAY_ATTR.get(cat), "max", "Large screen")
        if re.search(r"\bcompact\b|\bsmall\b(?!\s*budget)", low) and cat in {"smartphones", "laptops", "tablets"}:
            self._pref(intent, DISPLAY_ATTR.get(cat), "min", "Compact size")
        if re.search(r"(more|lots of|plenty of|max(imum)?)\s+storage", low):
            self._pref(
                intent, "storage_gb" if cat in {"laptops", "smartphones", "tablets"} else None, "max", "More storage"
            )
        if re.search(r"powerful|performance|fast\b|multitask\w*|heavy (use|work)", low) and cat in {
            "laptops",
            "smartphones",
            "tablets",
        }:
            self._pref(intent, "ram_gb", "max", "Performance / more RAM", 0.8)
        if cat == "speakers" and re.search(r"\bloud\b|bass|powerful|party", low):
            self._pref(intent, "output_w", "max", "Loud output")
        if cat == "monitors" and re.search(r"low latency|fast response|esports|competitive", low):
            self._pref(intent, "response_ms", "min", "Fast response time")

    def _soft_flag(self, intent: ShoppingIntent, attribute: str, label: str) -> None:
        if any(c.attribute == attribute for c in intent.constraints):
            return
        intent.constraints.append(Constraint(attribute=attribute, op="eq", value=True, label=label, hard=False))

    def _use_cases(self, low: str, intent: ShoppingIntent) -> None:
        cat = intent.category or ""
        uc: list[str] = []
        if re.search(
            r"machine learning|deep learning|\bml\b|\bai\b|data science|neural net\w*|training models?|pytorch|tensorflow|\bllms?\b|\bcuda\b",
            low,
        ):
            uc.append("machine learning")
            if cat == "laptops":
                self._soft_flag(intent, "dedicated_gpu", "Dedicated GPU (helps with machine learning)")
                self._pref(intent, "ram_gb", "max", "More RAM for ML workloads")
        if re.search(r"\bgam(e|es|ing|er)\b|esports|\bfps\b", low):
            uc.append("gaming")
            if cat == "laptops":
                self._soft_flag(intent, "dedicated_gpu", "Dedicated GPU (for gaming)")
                self._pref(intent, "refresh_hz", "max", "High refresh rate")
            elif cat == "monitors":
                self._pref(intent, "refresh_hz", "max", "High refresh rate", 1.5)
                self._pref(intent, "response_ms", "min", "Fast response time")
            elif cat == "smartphones":
                self._pref(intent, "ram_gb", "max", "More RAM for gaming")
                self._pref(intent, "refresh_hz", "max", "High refresh rate")
            elif cat == "mice":
                self._pref(intent, "dpi", "max", "High DPI sensor")
                self._pref(intent, "weight_g", "min", "Lightweight")
            elif cat == "keyboards":
                self._soft_flag(intent, "mechanical", "Mechanical switches (for gaming)")
        if re.search(r"college|student|school|studies|studying|university|campus|class(es)?\b|assignments?", low):
            uc.append("study")
            if cat == "laptops":
                self._pref(intent, "battery_hours", "max", "Long battery life", 1.0)
                self._pref(intent, "weight_kg", "min", "Easy to carry", 0.7)
            elif cat == "tablets":
                self._soft_flag(intent, "stylus_support", "Stylus support (for notes)")
        if re.search(r"office|work\b|productivity|coding|programming|developer|software dev\w*|spreadsheets?", low):
            uc.append("work")
            if cat == "laptops":
                self._pref(intent, "ram_gb", "max", "More RAM for multitasking", 0.8)
            elif cat == "monitors":
                self._soft_flag(intent, "usb_c", "USB-C (single-cable laptop setup)")
        if re.search(
            r"video editing|photo editing|content creat\w*|creator|graphic design|3d|rendering|premiere|davinci", low
        ):
            uc.append("creative work")
            if cat == "laptops":
                self._soft_flag(intent, "dedicated_gpu", "Dedicated GPU (faster rendering)")
                self._pref(intent, "ram_gb", "max", "More RAM for editing")
        if re.search(r"travel\w*|commut\w*|flights?|metro|train journeys?", low):
            uc.append("travel")
            if cat == "headphones":
                self._soft_flag(intent, "anc", "Noise cancelling (for travel)")
                self._pref(intent, "battery_hours", "max", "Long battery life")
            elif cat == "laptops":
                self._pref(intent, "weight_kg", "min", "Lightweight for travel")
        if re.search(r"running|\brun\b|fitness|workout|gym|cycling|marathon|hiking|trek\w*", low):
            uc.append("fitness")
            if cat == "smartwatches":
                self._soft_flag(intent, "gps", "Built-in GPS (for outdoor workouts)")
                self._pref(intent, "battery_days", "max", "Long battery life")
        if re.search(r"note[- ]?taking|notes|drawing|sketch\w*|illustrat\w*", low):
            uc.append("notes & drawing")
            if cat == "tablets":
                self._soft_flag(intent, "stylus_support", "Stylus support")
        if re.search(r"movies|netflix|streaming|binge|media consumption|youtube", low) and cat == "tablets":
            uc.append("media")
            self._pref(intent, "display_inches", "max", "Large screen for media")
        if re.search(r"party|outdoor|beach|pool|camping", low) and cat == "speakers":
            uc.append("outdoor")
            self._soft_flag(intent, "waterproof", "Waterproof (for outdoors)")
            self._pref(intent, "output_w", "max", "Loud output")
        intent.use_cases = list(dict.fromkeys(uc))

    # ------------------------------------------------------------------ rating
    def _rating(self, low: str, intent: ShoppingIntent) -> None:
        m = re.search(
            r"(?:rated|rating(?: of)?|at least|minimum|min|above|over)?\s*(\d(?:\.\d)?)\s*\+?\s*(?:stars?|★|star rating|/\s*5)|rated\s*(?:above|at least|over)?\s*(\d(?:\.\d)?)",
            low,
        )
        if m:
            r = float(m.group(1) or m.group(2))
            if 1 <= r <= 5:
                intent.constraints.append(Constraint(attribute="rating", op="gte", value=r, label=f"Rating ≥ {r:g}★"))

    # ------------------------------------------------------------------ cart & references
    def _cart(self, low: str, intent: ShoppingIntent) -> None:
        refs = r"(it|this|that|them|these|those|this one|that one|the (?:first|second|third|top|best|cheapest|last) (?:one|option|pick)|both)"
        if re.search(r"\b(add|put|throw|place)\b.{0,60}\b(cart|basket|bag|trolley)\b", low) or re.search(
            rf"\b(buy|purchase|order|i'?ll take|get me)\s+(\d+|two|three|a pair of|a couple of)?\s*(of\s+)?{refs}", low
        ):
            intent.wants_cart_add = True
        qty = re.search(
            r"\b(-?\d{1,4})\s*(?:units?|pieces?|pcs|nos|of (?:them|it|these|those|the))\b"
            r"|\b(?:add|buy|order|get|take|put)\s+(-?\d{1,4})\b(?!\s*(?:gb|tb|k\b|inch|hz|₹|rs))"
            r"|\b(?:quantity|qty)\s*(?:of|:|=|to)?\s*(-?\d{1,4})",
            low,
        )
        if qty:
            n = int(qty.group(1) or qty.group(2) or qty.group(3))
            if n <= 0:
                intent.keywords.append("__invalid_quantity__")
            else:
                intent.cart_quantity = n
        else:
            for word, n in _NUMBER_WORDS.items():
                if re.search(rf"\b(add|buy|order|get|take)\s+{word}\b", low):
                    intent.cart_quantity = n
        if intent.wants_cart_add:
            m = re.search(
                r"(?:\bthe\s+|#|\boption\s+|\bnumber\s+|\bno\.?\s*)(first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th|top|best|[1-5])\b",
                low,
            )
            if m:
                tok = m.group(1)
                intent.reference_index = int(tok) - 1 if tok.isdigit() else _ORDINALS[tok]
            elif re.search(r"\b(it|this|that)( one)?\b", low):
                intent.reference_index = 0
            if re.search(r"\bcheapest\b", low) and not intent.category:
                intent.keywords.append("__cheapest_ref__")

    def _sort_hint(self, low: str, intent: ShoppingIntent) -> None:
        if re.search(
            r"cheap(est|er)?\b|affordable|inexpensive|lowest price|budget[- ]friendly|least expensive|less expensive|value for money",
            low,
        ):
            intent.sort_hint = "cheapest"
            if re.search(r"\bcheapest\b|lowest[- ]price|least expensive", low):
                intent.keywords.append("__cheapest__")  # superlative: order matches strictly by price
        elif re.search(r"best[- ]rated|top[- ]rated|highest[- ]rated|best reviews|most reliable|highly rated", low):
            intent.sort_hint = "best_rated"
        if re.search(r"\bcheaper\b|less expensive|lower price|lower budget", low):
            intent.keywords.append("__cheaper__")

    # ------------------------------------------------------------------ alternatives policy
    _NEG = r"(?:do\s*n[o']t|don't|dont|never|no|not|nothing|without|avoid|stop)"
    _OVER_BUDGET = (
        r"(?:above|over|beyond|more than|exceed\w*|outside|higher than)\s+(?:my\s+|the\s+|this\s+|that\s+)?"
        r"(?:budget|price\s+(?:limit|cap|range)|limit|max(?:imum)?)|over[- ]budget|above[- ]budget|pricier|more expensive"
    )

    # Explicit permission: "I don't mind going over budget", "no problem with alternatives", "… is fine".
    _PERMIT = (
        r"\b(?:do\s*n[o']t|don't|dont|wouldn't|won't)\s+mind\b|\b(?:do\s*n[o']t|don't|dont)\s+worry\b"
        r"|\b(?:not|no)\s+(?:a\s+|any\s+)?problem\b|\bdoes\s*n[o']?t\s+matter\b"
        r"|\b(?:is|are|'s|'re)\s+(?:fine|ok(?:ay)?|allowed|acceptable)\b|\b(?:fine|ok(?:ay)?|happy)\s+with\b"
        r"|\b(?:show|include|suggest|give)\b[^.;!?]{0,25}\balternatives?\b"
    )
    _ALT_TOPIC = r"\balternatives?\b|over[- ]budget|above[- ]budget|\b(?:over|above|beyond)\s+(?:my\s+|the\s+)?budget\b|slightly over"
    # Clause boundaries; ", " (with a space) so "₹50,000" stays whole.
    _CLAUSE = re.compile(r"[.;!?]|,\s|\bbut\b|\bhowever\b|\bthough\b")

    def _alternatives_policy(self, low: str, intent: ShoppingIntent) -> None:
        """Explicit wording about what may be shown when nothing matches every requirement.

        Each clause is read on its own ("alternatives are fine, but nothing above my budget" has two);
        permission wins within a clause, and the strictest instruction wins across clauses.
        """
        rank: dict[AlternativesPolicy, int] = {"any": 0, "within_budget": 1, "none": 2}
        found: list[AlternativesPolicy] = []
        for clause in self._CLAUSE.split(low):
            policy = self._clause_policy(clause)
            if policy is not None:
                found.append(policy)
        if found:
            strictest: AlternativesPolicy = max(found, key=rank.__getitem__)
            intent.alternatives_policy = strictest

    def _clause_policy(self, clause: str) -> AlternativesPolicy | None:
        neg = self._NEG
        negated_show = re.search(rf"\b{neg}\b[^.;!?]{{0,15}}\b(?:show|include|suggest|give)\b", clause)
        if re.search(self._PERMIT, clause) and re.search(self._ALT_TOPIC, clause) and not negated_show:
            return "any"
        targets_budget = re.search(rf"\b{neg}\b[^.;!?]{{0,40}}(?:{self._OVER_BUDGET})", clause)
        if not targets_budget and re.search(
            rf"\b{neg}\b[^.;!?]{{0,30}}\b(?:alternatives?|suggestions?|substitutes?|other options)\b"
            r"|\bonly\s+(?:show\s+)?(?:exact|perfect|strict)\s+match(?:es)?\b|\bexact\s+matches\s+only\b",
            clause,
        ):
            return "none"
        if targets_budget or re.search(
            r"\bstrict(?:ly)?\s+(?:under|below|within|budget)\b|\bhard\s+(?:budget|limit|cap)\b"
            r"|\b(?:must|should)\s*n[o']?t\s+exceed\b|\bnot\s+a\s+(?:rupee|single\s+rupee)\s+more\b",
            clause,
        ):
            return "within_budget"
        return None

    # ------------------------------------------------------------------ intent kind
    def _kind(self, low: str, intent: ShoppingIntent) -> None:
        if re.search(
            r"^(start over|reset|new search|clear (the )?(chat|context|conversation))\b|\bforget (that|everything|it)\b|\bstart (again|fresh|over)\b",
            low,
        ):
            intent.kind = "reset"
            return
        pref = self._preference_statement(low, intent)
        if pref:
            intent.preference_to_save = pref
        if re.search(r"\bcompare\b|\bvs\.?\b|\bversus\b|difference between|which is better", low):
            intent.kind = "compare"
            return
        has_shopping_signal = bool(intent.category or intent.constraints or intent.budget_max or intent.preferences)
        if intent.wants_cart_add and not intent.category:
            intent.kind = "cart_add_reference"
            return
        if pref and not intent.category:
            intent.kind = "save_preference"
            return
        if not has_shopping_signal and re.search(
            r"^(hi|hello|hey|namaste|good (morning|evening|afternoon))\b|what can you do|\bhelp\b|how does this work|who are you",
            low,
        ):
            intent.kind = "help"
            return
        intent.kind = "search"

    def _preference_statement(self, low: str, intent: ShoppingIntent) -> PreferenceProposal | None:
        # Only explicit statements about the shopper themselves, not any sentence containing "always".
        if not re.search(
            r"\bremember\b|\bsave (this|my|that)\b|\bi (always|usually|never|only|mostly) (buy|prefer|choose|go for|use|like|want|shop|pick|keep)\b"
            r"|\b(always|never) (buy|prefer|choose|go for|pick)\b|\bfrom now on,? (i|my)\b|\bmy (usual|typical)\b|\balways keep my\b",
            low,
        ):
            return None
        for brand, pos in self._brand_mentions(low):
            before = low[max(0, pos - 28) : pos]
            if _NEGATION.search(before):
                return PreferenceProposal(kind="avoided_brand", value=brand)
            return PreferenceProposal(kind="preferred_brand", value=brand)
        if intent.budget_max and intent.category:
            return PreferenceProposal(kind="budget_max", value=str(intent.budget_max), category=intent.category)
        if intent.use_cases:
            return PreferenceProposal(kind="use_case", value=intent.use_cases[0])
        m = re.search(r"remember (?:that )?(.{3,200})", low)
        if m:
            return PreferenceProposal(kind="note", value=m.group(1).strip(" .!"))
        return None

    # ------------------------------------------------------------------ cleanup
    def _dedupe(self, intent: ShoppingIntent) -> None:
        seen: dict[tuple[str, str, bool], Constraint] = {}
        for c in intent.constraints:
            key = (c.attribute, c.op, c.hard)
            seen[key] = c  # later mentions win
        hard_attrs = {(c.attribute) for c in seen.values() if c.hard and c.op == "eq"}
        intent.constraints = [c for c in seen.values() if c.hard or c.attribute not in hard_attrs]


def describe_intent(intent: ShoppingIntent) -> list[str]:
    """Human-readable chips for 'What I understood'."""
    from app.services.specs import CATEGORY_LABELS

    chips: list[str] = []
    if intent.category:
        chips.append(CATEGORY_LABELS.get(intent.category, intent.category))
    if intent.budget_min and intent.budget_max:
        chips.append(f"{format_inr(intent.budget_min)} – {format_inr(intent.budget_max)}")
    elif intent.budget_max:
        chips.append(f"≤ {format_inr(intent.budget_max)}")
    elif intent.budget_min:
        chips.append(f"≥ {format_inr(intent.budget_min)}")
    for c in intent.constraints:
        tag = "required" if c.hard else "preferred"
        if c.source == "preference":
            tag = "saved preference"
        chips.append(f"{c.label or c.attribute} ({tag})")
    for p in intent.preferences:
        chips.append(f"{p.label} (preferred)")
    for b in intent.preferred_brands:
        chips.append(f"Prefers {b}")
    if intent.use_cases:
        chips.append("Use: " + ", ".join(intent.use_cases))
    if intent.sort_hint == "cheapest":
        chips.append("Lowest price first")
    elif intent.sort_hint == "best_rated":
        chips.append("Best rated first")
    if intent.alternatives_policy == "within_budget":
        chips.append("Strict: exact matches only")
    elif intent.alternatives_policy == "none":
        chips.append("Exact matches only")
    return list(dict.fromkeys(chips))
