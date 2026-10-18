from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from tests.conftest import register


def test_health(client: TestClient) -> None:
    assert client.get("/api/health").json() == {"status": "ok"}
    ready = client.get("/api/health/ready").json()
    assert ready["status"] == "ready" and ready["products"] == 115


def test_register_login_me(client: TestClient) -> None:
    r = client.post(
        "/api/auth/register", json={"email": "Asha@Example.com", "password": "Passw0rd!x", "display_name": " Asha  R "}
    )
    assert r.status_code == 201
    body = r.json()
    assert body["user"]["email"] == "asha@example.com" and body["user"]["display_name"] == "Asha R"
    assert "password" not in str(body)
    login = client.post("/api/auth/login", json={"email": "asha@example.com", "password": "Passw0rd!x"})
    assert login.status_code == 200
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {login.json()['access_token']}"})
    assert me.json()["email"] == "asha@example.com"


def test_duplicate_email_rejected(client: TestClient) -> None:
    register(client, "dup@example.com")
    r = client.post(
        "/api/auth/register", json={"email": "DUP@example.com", "password": "Passw0rd!x", "display_name": "x"}
    )
    assert r.status_code == 409


@pytest.mark.parametrize("password", ["short1", "alllettersonly", "12345678901", "aaaa1111"])
def test_weak_passwords_rejected(client: TestClient, password: str) -> None:
    r = client.post("/api/auth/register", json={"email": "w@example.com", "password": password, "display_name": "x"})
    assert r.status_code == 422


def test_wrong_password_and_unknown_user_look_identical(client: TestClient) -> None:
    register(client, "known@example.com")
    a = client.post("/api/auth/login", json={"email": "known@example.com", "password": "wrong-pass1"})
    b = client.post("/api/auth/login", json={"email": "nobody@example.com", "password": "wrong-pass1"})
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_password_is_hashed_with_argon2(client: TestClient, db) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    from app.models import User

    register(client, "hash@example.com", password="Passw0rd!x")
    user = db.scalar(select(User).where(User.email == "hash@example.com"))
    assert user.password_hash.startswith("$argon2id$") and "Passw0rd" not in user.password_hash


@pytest.mark.parametrize(
    "header",
    ["", "Bearer", "Bearer not-a-jwt", "Basic dXNlcjpwYXNz"],
)
def test_protected_routes_require_valid_token(client: TestClient, header: str) -> None:
    headers = {"Authorization": header} if header else {}
    for path in ["/api/cart", "/api/preferences", "/api/conversations", "/api/orders", "/api/actions"]:
        assert client.get(path, headers=headers).status_code == 401


def test_expired_and_forged_tokens_rejected(client: TestClient) -> None:
    s = get_settings()
    now = datetime.now(UTC)
    expired = jwt.encode(
        {"sub": "1", "iat": now - timedelta(hours=2), "exp": now - timedelta(hours=1), "typ": "access"},
        s.jwt_secret,
        "HS256",
    )
    forged = jwt.encode(
        {"sub": "1", "iat": now, "exp": now + timedelta(hours=1), "typ": "access"},
        "attacker-secret-" + "y" * 30,
        "HS256",
    )
    none_alg = jwt.encode(
        {"sub": "1", "iat": now, "exp": now + timedelta(hours=1), "typ": "access"}, None, algorithm="none"
    )
    for tok in (expired, forged, none_alg):
        assert client.get("/api/cart", headers={"Authorization": f"Bearer {tok}"}).status_code == 401


def test_login_rate_limited(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "rate_limit_enabled", True)
    monkeypatch.setattr(s, "rate_limit_auth_per_minute", 3)
    codes = [
        client.post("/api/auth/login", json={"email": "x@example.com", "password": "nope-nope1"}).status_code
        for _ in range(5)
    ]
    assert codes[:3] == [401, 401, 401] and codes[3:] == [429, 429]


def test_security_headers(client: TestClient) -> None:
    h = client.get("/api/health").headers
    assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"


def test_oversized_body_rejected(client: TestClient, auth: dict[str, str]) -> None:
    r = client.post(
        "/api/chat",
        headers={**auth, "Content-Type": "application/json"},
        content=b'{"message": "' + b"a" * 70000 + b'"}',
    )
    assert r.status_code == 413


# ------------------------------------------------------------------ catalogue
def test_catalog_counts_and_categories(client: TestClient) -> None:
    cats = client.get("/api/categories").json()
    assert len(cats) == 9 and sum(c["product_count"] for c in cats) == 115
    assert all(c["product_count"] >= 6 for c in cats)


def test_search_filters_sort_and_pagination(client: TestClient) -> None:
    r = client.get(
        "/api/products",
        params={
            "category": "laptops",
            "max_price": 70000,
            "min_rating": 4.2,
            "in_stock": True,
            "sort": "price_asc",
            "page_size": 5,
        },
    ).json()
    prices = [p["price_inr"] for p in r["items"]]
    assert prices == sorted(prices) and all(p <= 70000 for p in prices)
    assert all(p["rating"] >= 4.2 and p["in_stock"] for p in r["items"])
    assert r["pages"] == -(-r["total"] // 5)
    page2 = client.get(
        "/api/products",
        params={
            "category": "laptops",
            "max_price": 70000,
            "min_rating": 4.2,
            "in_stock": True,
            "sort": "price_asc",
            "page_size": 5,
            "page": 2,
        },
    ).json()
    assert not {p["id"] for p in r["items"]} & {p["id"] for p in page2["items"]}


def test_keyword_search_and_brand_filter(client: TestClient) -> None:
    r = client.get("/api/products", params={"q": "noise cancelling"}).json()
    assert r["total"] > 0 and all(p["category"] == "headphones" for p in r["items"])
    b = client.get("/api/products", params=[("brand", "Sony"), ("brand", "Bose")]).json()
    assert {p["brand"] for p in b["items"]} == {"Sony", "Bose"}
    hostile = client.get("/api/products", params={"q": "%_' OR 1=1; DROP TABLE products; --"})
    assert hostile.status_code == 200
    assert client.get("/api/products", params={"q": "zz%"}).json()["total"] == 0  # % is literal, not a wildcard
    assert client.get("/api/health/ready").json()["products"] == 115


def test_catalog_validation_errors(client: TestClient) -> None:
    assert client.get("/api/products", params={"category": "cars"}).status_code == 422
    assert client.get("/api/products", params={"min_price": 5000, "max_price": 100}).status_code == 422
    assert client.get("/api/products", params={"page_size": 500}).status_code == 422
    assert client.get("/api/products", params={"sort": "random"}).status_code == 422
    assert client.get("/api/products/999999").status_code == 404


def test_product_detail_and_compare(client: TestClient) -> None:
    d = client.get("/api/products/1").json()
    assert d["sku"] == "LAP-001" and d["description"] and any(s["key"] == "ram_gb" for s in d["specs"])
    cmp = client.get("/api/products/compare", params={"ids": "1,2,3"}).json()
    assert len(cmp["columns"]) == 3 and any(row["key"] == "battery_hours" for row in cmp["rows"])
    assert client.get("/api/products/compare", params={"ids": "1"}).status_code == 422
    assert client.get("/api/products/compare", params={"ids": "1,abc"}).status_code == 422
    assert client.get("/api/products/compare", params={"ids": "1,99999"}).status_code == 422
