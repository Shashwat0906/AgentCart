"""Category specification registry.

This is the single source of truth for which attributes exist per category, their types,
units and which direction is "better". The agent may only build constraints on attributes
registered here; anything else (e.g. hallucinated by an LLM) is dropped during validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SpecType = Literal["number", "bool", "text"]


@dataclass(frozen=True)
class SpecField:
    key: str
    label: str
    type: SpecType
    unit: str = ""
    # True: higher is better, False: lower is better, None: no natural ordering
    higher_is_better: bool | None = None
    min_value: float | None = None
    max_value: float | None = None


CATEGORY_LABELS: dict[str, str] = {
    "laptops": "Laptops",
    "smartphones": "Smartphones",
    "headphones": "Headphones & Earbuds",
    "monitors": "Monitors",
    "tablets": "Tablets",
    "smartwatches": "Smartwatches",
    "keyboards": "Keyboards",
    "mice": "Mice",
    "speakers": "Bluetooth Speakers",
}

_N: SpecType = "number"
_B: SpecType = "bool"
_T: SpecType = "text"

CATEGORY_SPECS: dict[str, list[SpecField]] = {
    "laptops": [
        SpecField("cpu", "Processor", _T),
        SpecField("ram_gb", "RAM", _N, "GB", True, 2, 128),
        SpecField("storage_gb", "Storage", _N, "GB", True, 64, 8192),
        SpecField("storage_upgradeable", "Upgradeable storage", _B),
        SpecField("ram_upgradeable", "Upgradeable RAM", _B),
        SpecField("gpu", "Graphics", _T),
        SpecField("dedicated_gpu", "Dedicated GPU", _B),
        SpecField("display_inches", "Display", _N, "in", None, 10, 20),
        SpecField("refresh_hz", "Refresh rate", _N, "Hz", True, 30, 500),
        SpecField("battery_hours", "Battery life", _N, "h", True, 1, 30),
        SpecField("weight_kg", "Weight", _N, "kg", False, 0.5, 5),
        SpecField("os", "OS", _T),
    ],
    "smartphones": [
        SpecField("chipset", "Chipset", _T),
        SpecField("ram_gb", "RAM", _N, "GB", True, 1, 24),
        SpecField("storage_gb", "Storage", _N, "GB", True, 16, 2048),
        SpecField("expandable_storage", "Expandable storage", _B),
        SpecField("battery_mah", "Battery", _N, "mAh", True, 1000, 10000),
        SpecField("charging_w", "Charging", _N, "W", True, 5, 240),
        SpecField("main_camera_mp", "Main camera", _N, "MP", True, 2, 300),
        SpecField("display_inches", "Display", _N, "in", None, 4, 8),
        SpecField("refresh_hz", "Refresh rate", _N, "Hz", True, 30, 240),
        SpecField("has_5g", "5G", _B),
        SpecField("os", "OS", _T),
    ],
    "headphones": [
        SpecField("form_factor", "Type", _T),
        SpecField("wireless", "Wireless", _B),
        SpecField("anc", "Active noise cancelling", _B),
        SpecField("battery_hours", "Battery life", _N, "h", True, 1, 120),
        SpecField("driver_mm", "Driver", _N, "mm", True, 5, 60),
        SpecField("weight_g", "Weight", _N, "g", False, 1, 500),
    ],
    "monitors": [
        SpecField("size_inches", "Size", _N, "in", None, 15, 57),
        SpecField("resolution", "Resolution", _T),
        SpecField("refresh_hz", "Refresh rate", _N, "Hz", True, 30, 500),
        SpecField("panel", "Panel", _T),
        SpecField("response_ms", "Response time", _N, "ms", False, 0.01, 20),
        SpecField("usb_c", "USB-C", _B),
        SpecField("hdr", "HDR", _B),
    ],
    "tablets": [
        SpecField("ram_gb", "RAM", _N, "GB", True, 1, 16),
        SpecField("storage_gb", "Storage", _N, "GB", True, 32, 2048),
        SpecField("display_inches", "Display", _N, "in", None, 7, 14),
        SpecField("battery_hours", "Battery life", _N, "h", True, 1, 24),
        SpecField("stylus_support", "Stylus support", _B),
        SpecField("cellular", "Cellular", _B),
        SpecField("os", "OS", _T),
    ],
    "smartwatches": [
        SpecField("battery_days", "Battery life", _N, "days", True, 0.5, 30),
        SpecField("gps", "Built-in GPS", _B),
        SpecField("display", "Display", _T),
        SpecField("water_resistant", "Water resistant", _B),
        SpecField("compatibility", "Works with", _T),
    ],
    "keyboards": [
        SpecField("layout", "Layout", _T),
        SpecField("mechanical", "Mechanical", _B),
        SpecField("switch_type", "Switches", _T),
        SpecField("wireless", "Wireless", _B),
        SpecField("backlit", "Backlit", _B),
    ],
    "mice": [
        SpecField("dpi", "Max DPI", _N, "DPI", True, 400, 50000),
        SpecField("wireless", "Wireless", _B),
        SpecField("weight_g", "Weight", _N, "g", False, 20, 200),
        SpecField("buttons", "Buttons", _N, "", True, 2, 20),
        SpecField("ergonomic", "Ergonomic", _B),
    ],
    "speakers": [
        SpecField("battery_hours", "Battery life", _N, "h", True, 1, 100),
        SpecField("waterproof", "Waterproof", _B),
        SpecField("output_w", "Output", _N, "W", True, 1, 200),
    ],
}

# Attributes valid in every category (stored as columns rather than in `specs`).
COMMON_FIELDS: dict[str, SpecField] = {
    "price_inr": SpecField("price_inr", "Price", "number", "₹", False, 1, 10_000_000),
    "rating": SpecField("rating", "Rating", "number", "★", True, 0, 5),
    "brand": SpecField("brand", "Brand", "text"),
}


def spec_field(category: str | None, key: str) -> SpecField | None:
    if key in COMMON_FIELDS:
        return COMMON_FIELDS[key]
    if category is None:
        return None
    for f in CATEGORY_SPECS.get(category, []):
        if f.key == key:
            return f
    return None


def format_spec_value(field: SpecField, value: object) -> str:
    if value is None:
        return "—"
    if field.type == "bool":
        return "Yes" if value else "No"
    if field.key == "price_inr" and isinstance(value, int | float):
        return format_inr(int(value))
    if field.type == "number" and isinstance(value, int | float):
        num = int(value) if float(value).is_integer() else value
        if field.key in {"storage_gb"} and isinstance(num, int) and num >= 1024:
            return f"{num // 1024} TB" if num % 1024 == 0 else f"{num / 1024:.1f} TB"
        return f"{num} {field.unit}".strip() if field.unit not in {"in"} else f'{num}"'
    return str(value)


def format_inr(amount: int) -> str:
    """Format an integer rupee amount using the Indian digit grouping: ₹1,24,990."""
    sign = "-" if amount < 0 else ""
    s = str(abs(int(amount)))
    if len(s) <= 3:
        return f"{sign}₹{s}"
    head, tail = s[:-3], s[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return f"{sign}₹{','.join(groups)},{tail}"


def cart_proposal_summary(quantity: int, name: str, unit_price_inr: int) -> str:
    """How a cart proposal is described everywhere. Budgets are per item, so the per-item price is always
    shown; with more than one unit the total is shown too."""
    price = f"{format_inr(unit_price_inr)} each"
    if quantity > 1:
        price += f", {format_inr(unit_price_inr * quantity)} total"
    return f"Add {quantity} × {name} ({price}) to your cart"[:300]
