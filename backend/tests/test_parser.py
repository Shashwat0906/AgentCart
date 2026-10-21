from __future__ import annotations

import pytest

from app.agents.parser import RequestParser, describe_intent, strip_budget_phrases

P = RequestParser()


def constraint(intent, attribute, op=None):  # type: ignore[no-untyped-def]
    return next((c for c in intent.constraints if c.attribute == attribute and (op is None or c.op == op)), None)


@pytest.mark.parametrize(
    ("text", "bmin", "bmax"),
    [
        ("laptop under ₹70,000", None, 70000),
        ("phone below 25k", None, 25000),
        ("tablet within Rs. 30000", None, 30000),
        ("laptop under 1.2 lakh", None, 120000),
        ("phone between 20k and 30k", 20000, 30000),
        ("20-30k phone", 20000, 30000),
        ("monitor at least 10k but under 20k", 10000, 20000),
        ("nothing over 50k, a phone", None, 50000),
        ("phone no more than 30000 rupees", None, 30000),
        ("phone above 30k", 30000, None),
        ("laptop for 45000", None, 45000),
        ("phone around 20k", 16000, 22000),
        ("phone with 5000 mAh battery", None, None),  # mAh must not be read as money
        ("laptop with i5-12450H and RTX 3050", None, None),
        ("4K monitor with USB-C", None, None),  # 4K is a resolution, not ₹4,000
        ("mouse under 4k", None, 4000),
    ],
)
def test_budget_parsing(text: str, bmin: int | None, bmax: int | None) -> None:
    i = P.parse(text)
    assert (i.budget_min, i.budget_max) == (bmin, bmax)


def test_reference_request_is_fully_understood() -> None:
    i = P.parse(
        "I need a laptop under ₹70,000 for machine learning and college work, with 16 GB RAM, "
        "good battery life, and upgradeable storage."
    )
    assert i.category == "laptops"
    assert i.budget_max == 70000
    ram = constraint(i, "ram_gb")
    assert ram and ram.op == "gte" and ram.value == 16 and ram.hard
    assert constraint(i, "storage_upgradeable").hard
    gpu = constraint(i, "dedicated_gpu")
    assert gpu and not gpu.hard  # ML implies a GPU *preference*, not a requirement
    assert any(p.attribute == "battery_hours" and p.direction == "max" for p in i.preferences)
    assert "machine learning" in i.use_cases


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("noise cancelling earbuds", "headphones"),
        ("headphones for my iphone", "headphones"),
        ("a mouse for my laptop", "mice"),
        ("27 inch gaming monitor", "monitors"),
        ("android tablet for netflix", "tablets"),
        ("running watch with gps", "smartwatches"),
        ("mechanical keyboard", "keyboards"),
        ("waterproof bluetooth speaker", "speakers"),
        ("macbook", "laptops"),
        ("something nice", None),
    ],
)
def test_category_detection(text: str, category: str | None) -> None:
    assert P.parse(text).category == category


def test_brand_include_exclude_and_word_brands() -> None:
    i = P.parse("Lenovo or HP laptop, no Acer")
    assert constraint(i, "brand", "in").value == ["Lenovo", "HP"]
    assert constraint(i, "brand", "not_in").value == ["Acer"]
    # "noise" in noise-cancelling and "nothing" as a word are not brands
    assert constraint(P.parse("noise cancelling headphones"), "brand") is None
    assert constraint(P.parse("nothing over 50k, a phone"), "brand") is None
    assert constraint(P.parse("Nothing Phone 2a"), "brand", "in").value == ["Nothing"]
    # a device mentioned as context does not become a brand requirement
    assert constraint(P.parse("headphones for my iPhone"), "brand") is None


def test_preferred_brand_is_soft() -> None:
    i = P.parse("phone under 30k, I prefer Samsung")
    assert i.preferred_brands == ["Samsung"]
    assert constraint(i, "brand") is None


def test_numeric_specs() -> None:
    i = P.parse("27 inch 144Hz QHD monitor")
    assert constraint(i, "refresh_hz").value == 144
    assert constraint(i, "size_inches", "gte").value == pytest.approx(26.4)
    assert constraint(i, "resolution").value == ["QHD", "UWQHD"]
    p = P.parse("phone with 8GB RAM, 256GB storage, 5000 mAh and 64MP camera")
    assert constraint(p, "ram_gb").value == 8
    assert constraint(p, "storage_gb").value == 256
    assert constraint(p, "battery_mah").value == 5000
    assert constraint(p, "main_camera_mp").value == 64


def test_soft_cue_makes_feature_optional() -> None:
    i = P.parse("headphones under 10k, noise cancelling would be nice")
    assert constraint(i, "anc").hard is False


def test_negated_feature() -> None:
    i = P.parse("wireless keyboard without backlight")
    assert constraint(i, "backlit").value is False


@pytest.mark.parametrize(
    ("text", "kind", "index", "qty"),
    [
        ("add the second one to my cart", "cart_add_reference", 1, 1),
        ("add the top pick to cart", "cart_add_reference", 0, 1),
        ("add 3 of them to cart", "cart_add_reference", None, 3),
        ("buy two of the first one", "cart_add_reference", 0, 2),
        ("put option 3 in my cart", "cart_add_reference", 2, 1),
    ],
)
def test_cart_references(text: str, kind: str, index: int | None, qty: int) -> None:
    i = P.parse(text)
    assert (i.kind, i.reference_index, i.cart_quantity) == (kind, index, qty)


def test_invalid_quantity_flagged() -> None:
    assert "__invalid_quantity__" in P.parse("add -2 to cart").keywords
    assert "__invalid_quantity__" in P.parse("set quantity to 0 and add to cart").keywords


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("hi", "help"),
        ("what can you do?", "help"),
        ("start over", "reset"),
        ("compare the first two", "compare"),
        ("Sony WH-1000XM5 vs Bose QuietComfort Ultra", "compare"),
        ("remember that I prefer Lenovo", "save_preference"),
        ("I never buy HP, remember that", "save_preference"),
        ("laptop under 50k", "search"),
    ],
)
def test_intent_kinds(text: str, kind: str) -> None:
    assert P.parse(text).kind == kind


def test_preference_proposals() -> None:
    assert P.parse("remember that I prefer Lenovo").preference_to_save.model_dump() == {
        "kind": "preferred_brand",
        "value": "Lenovo",
        "category": None,
    }
    assert P.parse("I never buy HP, remember that").preference_to_save.kind == "avoided_brand"
    assert P.parse("always keep my laptop budget under 60k").preference_to_save.model_dump() == {
        "kind": "budget_max",
        "value": "60000",
        "category": "laptops",
    }


def test_describe_intent_has_no_duplicates() -> None:
    chips = describe_intent(P.parse("27 inch monitor"))
    assert len(chips) == len(set(chips))


@pytest.mark.parametrize(
    ("text", "keywords"),
    [
        ("laptop under 60000", "laptop"),
        ("laptops under ₹60,000", "laptops"),
        ("Under ₹60,000 Laptop", "Laptop"),
        ("laptop below 60k", "laptop"),
        ("laptop for rs 60000", "laptop"),
        ("laptop around ₹60,000", "laptop"),
        ("laptop above 60000", "laptop"),
        ("laptop nothing over 60000", "laptop"),
        ("laptop between 40000 and 60000", "laptop"),
        ("laptop 40-60k", "laptop"),
        ("laptop under 60000 with 16gb ram", "laptop with 16gb ram"),
        # not budgets: units, resolutions and plain words are kept
        ("phone 128gb", "phone 128gb"),
        ("4k monitor", "4k monitor"),
        ("noise cancelling", "noise cancelling"),
        ("under 60000", ""),
    ],
)
def test_strip_budget_phrases_removes_exactly_what_the_parser_reads_as_a_budget(text: str, keywords: str) -> None:
    assert strip_budget_phrases(text) == keywords
    stripped = RequestParser().parse(keywords) if keywords else None
    assert stripped is None or (stripped.budget_max is None and stripped.budget_min is None)
