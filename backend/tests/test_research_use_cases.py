"""Research tasks rank by the query's use case instead of requiring use-case words as keywords.

Regression (production): category Laptops, "need a laptop for coding", ₹50,000 returned 0 matching products
although 4 in-stock laptops cost at most ₹50,000. "coding" was a required search keyword, and no product's
text says "coding". The parser already reads it as a use case ("work": more RAM), which now ranks the
results; category, price and stock stay strict filters and nothing is invented.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.agents.guardrails import sanitize_user_text
from app.agents.parser import RequestParser, strip_use_case_phrases
from app.models import Product
from app.services.ranking import rank_products
from tests.conftest import register
from tests.test_tasks import fetch, start


def research(client: TestClient, h: dict[str, str], **inp: Any) -> dict[str, Any]:
    r = start(client, h, inp)
    assert r.status_code == 201, r.text
    return fetch(client, h, r.json()["id"])


def catalogue(client: TestClient, **params: Any) -> dict[int, dict[str, Any]]:
    """Ground truth from the public catalogue API (in stock)."""
    items = client.get("/api/products", params={"in_stock": "true", "page_size": 48, **params}).json()["items"]
    return {p["id"]: p for p in items}


def raw_specs(client: TestClient, pid: int) -> dict[str, Any]:
    return client.get(f"/api/products/{pid}").json()["raw_specs"]


# ------------------------------------------------------------------ the production query
def test_need_a_laptop_for_coding_under_50k_finds_the_catalogues_laptops(client: TestClient, db: Session) -> None:
    h = register(client)
    t = research(client, h, category="laptops", query="need a laptop for coding", max_price_inr=50_000)
    truth = catalogue(client, category="laptops", max_price=50_000)
    assert len(truth) == 4  # seeded catalogue: these exist, so 0 was wrong
    assert t["status"] == "succeeded"
    args = t["steps"][0]["args"]
    assert args["query"] == "laptop" and args["category"] == "laptops" and args["max_price_inr"] == 50_000
    assert args["in_stock"] is True  # strict filters unchanged
    res = t["result"]
    assert res["matches"] == len(truth) and res["ranked_for"] == ["work"]
    assert res["top"] and all(
        p["id"] in truth and p["price_inr"] <= 50_000 and p["category"] == "laptops" for p in res["top"]
    )
    assert res["all_within_budget"] is True

    # the order is the existing ranking's, for the parsed use case (coding -> "work": more RAM)
    intent = (
        RequestParser()
        .parse(sanitize_user_text("need a laptop for coding"))
        .model_copy(update={"category": "laptops", "budget_max": 50_000})
    )
    products = [db.get(Product, pid) for pid in truth]
    expected = [r.product.id for r in rank_products([p for p in products if p is not None], intent)]
    assert [p["id"] for p in res["top"]] == expected[: len(res["top"])]
    assert t["steps"][1]["args"]["product_ids"] == [p["id"] for p in res["top"]]  # compare uses the same order


@pytest.mark.parametrize(
    ("query", "use_case"),
    [
        ("i need laptop for coding", "work"),
        ("laptop for programming", "work"),
        ("a laptop for office work", "work"),
        ("laptop for software development", "work"),
        ("laptop for college", "study"),
        ("laptop for my university assignments", "study"),
    ],
)
def test_use_case_words_rank_instead_of_filtering(client: TestClient, query: str, use_case: str) -> None:
    h = register(client)
    t = research(client, h, category="laptops", query=query, max_price_inr=60_000)
    truth = catalogue(client, category="laptops", max_price=60_000)
    res = t["result"]
    assert res["matches"] == len(truth) == 7 and res["ranked_for"] == [use_case]
    assert all(p["id"] in truth and p["price_inr"] <= 60_000 for p in res["top"])


def test_coding_ranks_more_memory_first(client: TestClient) -> None:
    h = register(client)
    t = research(client, h, query="i need laptop for coding")  # the reported phrasing, no category or budget
    truth = catalogue(client, category="laptops")
    assert t["result"]["matches"] == len(truth) == 24
    top_ram = [raw_specs(client, p["id"])["ram_gb"] for p in t["result"]["top"]]
    assert top_ram[0] == max(p_ram for p_ram in (raw_specs(client, pid)["ram_gb"] for pid in truth))


def test_gaming_ranks_laptops_with_a_dedicated_gpu_first(client: TestClient) -> None:
    h = register(client)
    t = research(client, h, category="laptops", query="gaming laptop", max_price_inr=60_000)
    truth = catalogue(client, category="laptops", max_price=60_000)
    assert t["result"]["matches"] == len(truth) and t["result"]["ranked_for"] == ["gaming"]
    gpu = {pid: bool(raw_specs(client, pid).get("dedicated_gpu")) for pid in truth}
    top = [p["id"] for p in t["result"]["top"]]
    assert all(gpu[pid] for pid in top) and sum(gpu.values()) >= len(top)  # every top pick has one


# ------------------------------------------------------------------ genuine no-match: nothing invented, budget kept
def test_no_laptop_for_coding_under_20k_is_a_genuine_no_match(client: TestClient) -> None:
    h = register(client)
    assert catalogue(client, category="laptops", max_price=20_000) == {}
    t = research(client, h, category="laptops", query="need a laptop for coding", max_price_inr=20_000)
    assert t["steps"][0]["args"]["max_price_inr"] == 20_000  # the user's maximum, not relaxed
    assert t["result"]["matches"] == 0 and t["result"]["top"] == []
    assert [s["status"] for s in t["steps"]][:2] == ["succeeded", "skipped"]  # nothing to compare


def test_a_use_case_never_crosses_categories(client: TestClient) -> None:
    h = register(client)
    t = research(client, h, category="mice", query="need a laptop for coding")
    assert t["steps"][0]["args"]["category"] == "mice"
    assert t["result"]["matches"] == 0 and t["result"]["top"] == []


def test_a_typed_budget_still_caps_a_use_case_search(client: TestClient) -> None:
    h = register(client)
    t = research(client, h, category="laptops", query="need a laptop for coding under ₹40,000", max_price_inr=50_000)
    truth = catalogue(client, category="laptops", max_price=40_000)
    assert t["steps"][0]["args"]["max_price_inr"] == 40_000
    assert t["result"]["matches"] == len(truth) and all(p["price_inr"] <= 40_000 for p in t["result"]["top"])


def test_out_of_stock_laptops_stay_excluded(client: TestClient, db: Session) -> None:
    h = register(client)
    truth = catalogue(client, category="laptops", max_price=50_000)
    gone = db.get(Product, next(iter(truth)))
    assert gone is not None
    gone.stock = 0
    db.commit()
    t = research(client, h, category="laptops", query="need a laptop for coding", max_price_inr=50_000)
    assert t["result"]["matches"] == len(truth) - 1 and gone.id not in {p["id"] for p in t["result"]["top"]}


# ------------------------------------------------------------------ everything else is unchanged
@pytest.mark.parametrize(
    ("inp", "query"),
    [
        ({"category": "headphones", "query": "noise cancelling headphones"}, "noise cancelling headphones"),
        ({"category": "mice", "query": "wireless mouse"}, "wireless mouse"),
        ({"category": "laptops", "max_price_inr": 50_000}, None),
    ],
)
def test_queries_without_a_use_case_are_not_reranked(
    client: TestClient, inp: dict[str, Any], query: str | None
) -> None:
    h = register(client)
    t = research(client, h, **inp)
    args = t["steps"][0]["args"]
    assert args.get("query") == query and args["page_size"] == 12 and "ranked_for" not in t["result"]


@pytest.mark.parametrize(
    ("text", "use_cases", "kept"),
    [
        ("laptop for coding", ["work"], "laptop for"),
        ("need a laptop for college", ["study"], "need a laptop for"),
        ("gaming laptop", ["gaming"], "laptop"),
        ("laptop for programming and machine learning", ["machine learning", "work"], "laptop for and"),
        ("laptop for network engineering", ["work"], "laptop for network engineering"),  # whole words only
        ("noise cancelling headphones", [], "noise cancelling headphones"),
        ("laptop for coding", [], "laptop for coding"),  # only use cases the parser recognised
    ],
)
def test_strip_use_case_phrases(text: str, use_cases: list[str], kept: str) -> None:
    assert strip_use_case_phrases(text, use_cases) == kept


# ------------------------------------------------------------------ developer phrasing and "purposes"
CODING_PHRASES = [
    "need a laptop for coding",  # the original production report
    "need a laptop for coding purposes",
    "need a laptop for Python coding",
    "need a laptop for web development",
    "need a laptop for app development",
    "laptop for javascript and typescript",
    "I need a laptop for coding purpose",
]


@pytest.mark.parametrize("query", CODING_PHRASES)
def test_coding_phrasings_find_the_in_stock_laptops_under_50k(client: TestClient, query: str) -> None:
    h = register(client)
    t = research(client, h, category="laptops", query=query, max_price_inr=50_000)
    truth = catalogue(client, category="laptops", max_price=50_000)
    args = t["steps"][0]["args"]
    assert (args["category"], args["max_price_inr"], args["in_stock"]) == ("laptops", 50_000, True)  # strict filters
    res = t["result"]
    assert res["matches"] == len(truth) == 4 and res["ranked_for"] == ["work"]
    assert res["top"] and all(
        p["id"] in truth and p["category"] == "laptops" and p["price_inr"] <= 50_000 and p["in_stock"]
        for p in res["top"]
    )


@pytest.mark.parametrize("query", CODING_PHRASES[1:4])
def test_the_new_phrasings_keep_genuine_no_matches(client: TestClient, query: str) -> None:
    h = register(client)
    cheap = research(client, h, category="laptops", query=query, max_price_inr=20_000)
    assert cheap["steps"][0]["args"]["max_price_inr"] == 20_000 and cheap["result"]["matches"] == 0  # budget kept
    wrong = research(client, h, category="mice", query=query)
    assert wrong["steps"][0]["args"]["category"] == "mice" and wrong["result"]["matches"] == 0  # category kept


def test_the_new_phrasings_still_exclude_out_of_stock_laptops(client: TestClient, db: Session) -> None:
    h = register(client)
    truth = catalogue(client, category="laptops", max_price=50_000)
    gone = db.get(Product, next(iter(truth)))
    assert gone is not None
    gone.stock = 0
    db.commit()
    t = research(client, h, category="laptops", query="need a laptop for web development", max_price_inr=50_000)
    assert t["result"]["matches"] == len(truth) - 1 and gone.id not in {p["id"] for p in t["result"]["top"]}


def test_laptop_model_names_and_product_words_are_not_developer_terms(client: TestClient) -> None:
    h = register(client)
    swift = research(client, h, category="laptops", query="acer swift laptop")  # "Swift" is a model name
    assert swift["steps"][0]["args"]["query"] == "acer swift laptop" and "ranked_for" not in swift["result"]
    anc = research(client, h, category="headphones", query="noise cancelling headphones for python coding")
    assert anc["steps"][0]["args"]["query"] == "noise cancelling headphones"  # product words still filter


def test_the_shared_chat_parser_is_unchanged_by_research_phrasing() -> None:
    """The developer vocabulary is research-only: chat parses these exactly as before."""
    parse = RequestParser().parse
    assert parse("need a laptop for web development").use_cases == []
    assert parse("need a laptop for python").use_cases == []
    assert parse("need a laptop for coding purposes").use_cases == ["work"]  # "coding" itself was always known
