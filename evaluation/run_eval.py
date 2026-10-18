"""AgentCart deterministic evaluation suite.

Usage (from the repo root):
    backend/.venv/bin/python evaluation/run_eval.py [--report-dir evaluation/reports] [--strict]

* Agent scenarios run the real LangGraph workflow over the seeded catalogue (in memory).
* API scenarios run the real FastAPI app against a throw-away SQLite database.
No network access or API keys are needed; results are fully deterministic.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("ENVIRONMENT", "test")
os.environ["RATE_LIMIT_ENABLED"] = "false"
os.environ["LLM_PROVIDER"] = "mock"
os.environ["DATABASE_URL"] = "sqlite://"
os.environ.setdefault("JWT_SECRET", "eval-only-secret-" + "e" * 40)

from app.agents.graph import AgentDeps, AgentResult, run_agent  # noqa: E402
from app.agents.tools import InMemoryCatalog, ToolBox, snapshots_from_seed  # noqa: E402
from app.providers import MockProvider  # noqa: E402
from app.providers.base import LLMProvider  # noqa: E402
from app.services.chat import next_context  # noqa: E402

MAX_ITEM_QUANTITY = 10
THRESHOLDS = {
    "scenario_pass_rate": 0.95,
    "constraint_satisfaction_rate": 1.0,
    "invalid_action_rate": 0.0,
    "tool_call_success_rate": 0.99,
    "grounded_card_rate": 1.0,
}

SNAPS = snapshots_from_seed()
BY_ID = {p.id: p for p in SNAPS}
BY_SKU = {p.sku: p for p in SNAPS}


class ScriptedLLM(LLMProvider):
    """Stands in for a real model so 'what if the LLM misbehaves' is testable offline."""

    name = "scripted"

    def __init__(self, json_out: str = "{}", text_out: str = "") -> None:
        super().__init__(timeout=1, max_retries=0)
        self.json_out, self.text_out = json_out, text_out

    def _complete(self, system: str, user: str, *, json_mode: bool, timeout: float) -> str:
        return self.json_out if json_mode else self.text_out


# ============================================================================ agent scenarios


def satisfies(product: Any, gt: dict[str, Any]) -> list[str]:
    """Check a product against independent ground truth. Returns violated rules."""
    bad: list[str] = []

    def val(attr: str) -> Any:
        return getattr(product, attr) if attr in {"price_inr", "rating", "brand", "stock"} else product.specs.get(attr)

    if gt.get("category") and product.category != gt["category"]:
        bad.append("category")
    if gt.get("max_price") is not None and product.price_inr > gt["max_price"]:
        bad.append("max_price")
    if gt.get("min_price") is not None and product.price_inr < gt["min_price"]:
        bad.append("min_price")
    for a, v in (gt.get("gte") or {}).items():
        if val(a) is None or val(a) < v:
            bad.append(f"{a}>={v}")
    for a, v in (gt.get("lte") or {}).items():
        if val(a) is None or val(a) > v:
            bad.append(f"{a}<={v}")
    for a, v in (gt.get("eq") or {}).items():
        if val(a) != v:
            bad.append(f"{a}=={v}")
    for a, vs in (gt.get("in") or {}).items():
        if val(a) not in vs:
            bad.append(f"{a} in {vs}")
    for a, vs in (gt.get("not_in") or {}).items():
        if val(a) in vs:
            bad.append(f"{a} not in {vs}")
    if product.stock <= 0:
        bad.append("in_stock")
    return bad


def run_turns(sc: dict[str, Any]) -> tuple[AgentResult, AgentResult | None, list[dict[str, Any]]]:
    context: dict[str, Any] = {}
    if sc.get("context_skus"):
        context = {"last_recommendation_ids": [BY_SKU[s].id for s in sc["context_skus"]]}
    prefs = sc.get("saved_preferences") or []
    prev: AgentResult | None = None
    result: AgentResult | None = None
    all_tool_calls: list[dict[str, Any]] = []
    turns = sc["turns"]
    for i, msg in enumerate(turns):
        last = i == len(turns) - 1
        provider: LLMProvider = MockProvider()
        if last and sc.get("llm"):
            text = sc["llm"].get("text", "")
            if "{top1}" in text:  # resolve a grounded template against a mock run first
                probe = run_agent(msg, AgentDeps(tools=ToolBox(InMemoryCatalog(SNAPS)), provider=MockProvider()), context=context, saved_preferences=prefs)
                text = text.replace("{top1}", probe.recommendations[0]["name"])
            provider = ScriptedLLM(sc["llm"].get("json", "{}"), text)
        deps = AgentDeps(tools=ToolBox(InMemoryCatalog(SNAPS)), provider=provider, max_item_quantity=MAX_ITEM_QUANTITY)
        res = run_agent(msg, deps, context=context, saved_preferences=prefs, weights=sc.get("weights"))
        all_tool_calls += res.tool_calls
        if not last:
            prev = res
        result = res
        context = next_context(res, context)
    assert result is not None
    return result, prev, all_tool_calls


def invalid_actions(result: AgentResult) -> list[str]:
    problems = []
    for a in result.proposed_actions:
        if a["kind"] not in {"add_to_cart", "save_preference"}:
            problems.append(f"unknown kind {a['kind']}")
            continue
        if a["kind"] != "add_to_cart":
            continue
        p = BY_ID.get(a["payload"]["product_id"])
        q = a["payload"]["quantity"]
        if p is None:
            problems.append("non-catalogue product")
        elif p.stock <= 0:
            problems.append(f"{p.sku} out of stock")
        elif not 1 <= q <= min(MAX_ITEM_QUANTITY, p.stock):
            problems.append(f"invalid quantity {q}")
        elif a["payload"]["expected_price_inr"] != p.price_inr:
            problems.append("price differs from catalogue")
    if result.proposed_actions and not result.awaiting_approval:
        problems.append("actions proposed without awaiting approval")
    return problems


def eval_agent_scenario(sc: dict[str, Any]) -> dict[str, Any]:
    t0 = time.perf_counter()
    res, prev, tool_calls = run_turns(sc)
    exp = sc["expect"]
    checks: dict[str, bool] = {}
    recs = res.recommendations
    rec_skus = [BY_ID[c["product_id"]].sku for c in recs]
    shown = recs or res.alternatives

    if "type" in exp:
        checks["type"] = res.response_type == exp["type"]
    gt_results = []
    if exp.get("ground_truth"):
        for c in recs:
            gt_results.append(not satisfies(BY_ID[c["product_id"]], exp["ground_truth"]))
        checks["ground_truth"] = bool(recs) and all(gt_results)
    if exp.get("top1_any"):
        checks["top1"] = bool(rec_skus) and rec_skus[0] in exp["top1_any"]
    if exp.get("top3_any"):
        checks["top3"] = bool(set(rec_skus[:3]) & set(exp["top3_any"]))
    if exp.get("must_not_recommend"):
        checks["must_not_recommend"] = not (set(rec_skus) & set(exp["must_not_recommend"]))
    if exp.get("not_in_top3"):
        checks["not_in_top3"] = not (set(rec_skus[:3]) & set(exp["not_in_top3"]))
    if exp.get("same_top3_as"):
        base = run_agent(exp["same_top3_as"], AgentDeps(tools=ToolBox(InMemoryCatalog(SNAPS)), provider=MockProvider()))
        checks["same_top3_as_baseline"] = rec_skus[:3] == [BY_ID[c["product_id"]].sku for c in base.recommendations[:3]]
    if exp.get("top1_tradeoff_contains"):
        checks["top1_tradeoff"] = bool(recs) and any(exp["top1_tradeoff_contains"] in t for t in recs[0]["tradeoffs"])
    if exp.get("security_flags"):
        checks["security_flags"] = bool(res.security_flags)
    if exp.get("notice_contains"):
        checks["notice"] = any(exp["notice_contains"].lower() in n.lower() for n in res.notices)
    if exp.get("summary_contains"):
        checks["summary_contains"] = exp["summary_contains"].lower() in res.summary.lower()
    if exp.get("summary_excludes"):
        checks["summary_excludes"] = not any(s.lower() in res.summary.lower() for s in exp["summary_excludes"])
    if exp.get("question_id"):
        checks["question"] = any(q.get("id") == exp["question_id"] for q in res.questions)
    if exp.get("no_recommendations"):
        checks["no_recommendations"] = not recs and not res.alternatives
    if exp.get("alternatives"):
        checks["alternatives"] = bool(res.alternatives) and all(a["violations"] and a["is_alternative"] for a in res.alternatives)
    if exp.get("min_recommendations"):
        checks["min_recommendations"] = len(recs) >= exp["min_recommendations"]
    if exp.get("comparison_columns"):
        checks["comparison"] = bool(res.comparison) and len(res.comparison["columns"]) == exp["comparison_columns"]
    if exp.get("comparison_skus"):
        cols = [BY_ID[c["product_id"]].sku for c in (res.comparison or {}).get("columns", [])]
        checks["comparison_skus"] = sorted(cols) == sorted(exp["comparison_skus"])
    if exp.get("trace_contains"):
        checks["trace"] = any(exp["trace_contains"] in t.get("detail", "") for t in res.trace)
    if exp.get("awaiting_approval"):
        checks["awaiting_approval"] = res.awaiting_approval
    if exp.get("cheaper_than_previous_top") and prev is not None:
        top_prev = prev.recommendations[0]["price_inr"]
        checks["cheaper"] = bool(recs) and all(c["price_inr"] < top_prev for c in recs)
    if "actions" in exp:
        ea, acts = exp["actions"], res.proposed_actions
        ok = len(acts) == ea["count"]
        if ok and ea.get("kind"):
            ok = acts[0]["kind"] == ea["kind"]
        if ok and ea.get("quantity"):
            ok = acts[0]["payload"]["quantity"] == ea["quantity"]
        if ok and ea.get("product"):
            target = ea["product"]
            if target == "top1":
                want = recs[0]["product_id"] if recs else None
            elif target.startswith("prev") and prev is not None:
                want = prev.recommendations[int(target[4:]) - 1]["product_id"]
            else:
                want = BY_SKU[target].id
            ok = acts[0]["payload"]["product_id"] == want
        checks["actions"] = ok

    # Global invariants checked on every scenario
    grounded = all(BY_ID[c["product_id"]].price_inr == c["price_inr"] and BY_ID[c["product_id"]].rating == c["rating"] for c in shown)
    checks["grounded_cards"] = grounded
    bad_actions = invalid_actions(res)
    checks["no_invalid_actions"] = not bad_actions
    checks["no_error"] = res.response_type != "error"

    return {
        "id": sc["id"], "category": sc["category"], "description": sc["description"], "kind": "agent",
        "passed": all(checks.values()), "checks": checks, "response_type": res.response_type,
        "top3": rec_skus[:3], "recommendations": len(recs), "gt_satisfied": sum(gt_results), "gt_total": len(gt_results),
        "top1_hit": checks.get("top1"), "top3_hit": checks.get("top3"),
        "actions_proposed": len(res.proposed_actions), "invalid_actions": bad_actions,
        "tool_calls": len(tool_calls), "tool_calls_ok": sum(1 for c in tool_calls if c["ok"]),
        "cards_checked": len(shown), "cards_grounded": len(shown) if grounded else 0,
        "ms": round((time.perf_counter() - t0) * 1000, 1),
    }


# ============================================================================ API scenarios


class Api:
    """Thin helper around TestClient with a fresh seeded database."""

    def __init__(self) -> None:
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from app.api.deps import get_provider
        from app.db.session import get_db
        from app.main import app
        from app.models import Base
        from app.seed import seed_catalog

        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.Session() as s:
            seed_catalog(s)

        def _db():  # type: ignore[no-untyped-def]
            s = self.Session()
            try:
                yield s
            finally:
                s.close()

        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[get_provider] = lambda: MockProvider()
        self.app = app
        self.c = TestClient(app)

    def close(self) -> None:
        self.app.dependency_overrides.clear()
        self.engine.dispose()

    def user(self) -> dict[str, str]:
        r = self.c.post("/api/auth/register", json={"email": f"eval-{uuid.uuid4().hex[:8]}@example.com", "password": "Eval-pass-123", "display_name": "Eval"})
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    def product(self, sku: str) -> Any:
        from sqlalchemy import select

        from app.models import Product

        s = self.Session()
        return s, s.scalar(select(Product).where(Product.sku == sku))

    def set_product(self, product_id: int, **fields: Any) -> None:
        from app.models import Product

        with self.Session() as s:
            p = s.get(Product, product_id)
            for k, v in fields.items():
                setattr(p, k, v)
            s.commit()

    def get_product(self, product_id: int) -> Any:
        from app.models import Product

        with self.Session() as s:
            return s.get(Product, product_id)

    def propose(self, h: dict[str, str]) -> dict[str, Any]:
        first = self.c.post("/api/chat", headers=h, json={"message": "laptop under 70k with 16GB RAM"}).json()
        out = self.c.post("/api/chat", headers=h, json={"message": "add the top pick to my cart", "conversation_id": first["conversation_id"]}).json()
        out["first"] = first
        return out

    def cart_count(self, h: dict[str, str]) -> int:
        return self.c.get("/api/cart", headers=h).json()["item_count"]

    def quote(self, h: dict[str, str], pid: int = 1, qty: int = 1) -> dict[str, Any]:
        self.c.post("/api/cart/items", headers=h, json={"product_id": pid, "quantity": qty})
        return self.c.post("/api/checkout/quote", headers=h).json()

    def confirm(self, h: dict[str, str], quote_id: str, key: str | None = None) -> Any:
        return self.c.post("/api/checkout/confirm", headers=h, json={"quote_id": quote_id, "confirm": True, "idempotency_key": key or uuid.uuid4().hex})


def x01(api: Api) -> dict[str, bool]:
    h = api.user()
    out = api.propose(h)
    before = api.cart_count(h)
    r = api.c.post(f"/api/actions/{out['actions'][0]['id']}/approve", headers=h)
    return {"pending_before": out["actions"][0]["status"] == "pending", "cart_empty_before": before == 0,
            "approved": r.status_code == 200, "cart_after": api.cart_count(h) == 1}


def x02(api: Api) -> dict[str, bool]:
    h = api.user()
    aid = api.propose(h)["actions"][0]["id"]
    rej = api.c.post(f"/api/actions/{aid}/reject", headers=h)
    again = api.c.post(f"/api/actions/{aid}/approve", headers=h)
    return {"rejected": rej.status_code == 200, "approve_after_reject_410": again.status_code == 410, "cart_empty": api.cart_count(h) == 0}


def x03(api: Api) -> dict[str, bool]:
    h = api.user()
    a = api.propose(h)["actions"][0]
    pid = a["payload"]["product_id"]
    new_price = a["payload"]["expected_price_inr"] + 2000
    api.set_product(pid, price_inr=new_price, mrp_inr=new_price + 5000)
    r = api.c.post(f"/api/actions/{a['id']}/approve", headers=h)
    d = r.json()["error"]["details"]
    cart_empty = api.cart_count(h) == 0
    fresh = api.c.post(f"/api/actions/{d['new_action_id']}/approve", headers=h)
    return {"conflict_409": r.status_code == 409, "reason_price_changed": d.get("reason") == "price_changed", "cart_untouched": cart_empty,
            "reapproval_uses_new_price": fresh.status_code == 200 and fresh.json()["cart"]["items"][0]["unit_price_inr"] == new_price}


def x04(api: Api) -> dict[str, bool]:
    h = api.user()
    a = api.propose(h)["actions"][0]
    api.set_product(a["payload"]["product_id"], stock=0)
    r = api.c.post(f"/api/actions/{a['id']}/approve", headers=h)
    return {"conflict_409": r.status_code == 409, "cart_empty": api.cart_count(h) == 0}


def x05(api: Api) -> dict[str, bool]:
    h = api.user()
    codes = {repr(q): api.c.post("/api/cart/items", headers=h, json={"product_id": 1, "quantity": q}).status_code for q in [0, -1, 2.5, "3", 11, True]}
    return {f"qty_{k}_rejected": v == 422 for k, v in codes.items()} | {"cart_empty": api.cart_count(h) == 0}


def x06(api: Api) -> dict[str, bool]:
    h = api.user()
    s, p = api.product("LAP-018")
    pid, stock = p.id, p.stock
    s.close()
    ok = api.c.post("/api/cart/items", headers=h, json={"product_id": pid, "quantity": stock})
    over = api.c.post("/api/cart/items", headers=h, json={"product_id": pid, "quantity": 1})
    return {"up_to_stock_ok": ok.status_code == 200, "oversell_409": over.status_code == 409}


def x07(api: Api) -> dict[str, bool]:
    h = api.user()
    q = api.quote(h)
    p = api.get_product(1)
    api.set_product(1, price_inr=p.price_inr + 500, mrp_inr=p.mrp_inr + 500)
    r = api.confirm(h, q["quote_id"])
    return {"conflict_409": r.status_code == 409, "reason": r.json()["error"]["details"].get("reason") == "price_changed",
            "no_order": api.c.get("/api/orders", headers=h).json() == []}


def x08(api: Api) -> dict[str, bool]:
    h = api.user()
    q = api.quote(h, 2, 3)
    api.set_product(2, stock=1)
    r = api.confirm(h, q["quote_id"])
    return {"conflict_409": r.status_code == 409, "stock_unchanged": api.get_product(2).stock == 1,
            "no_order": api.c.get("/api/orders", headers=h).json() == []}


def x09(api: Api) -> dict[str, bool]:
    h = api.user()
    q = api.quote(h)
    a, b = api.confirm(h, q["quote_id"], "eval-idem-key-1"), api.confirm(h, q["quote_id"], "eval-idem-key-1")
    return {"first_201": a.status_code == 201, "replay_200": b.status_code == 200, "same_order": a.json()["id"] == b.json()["id"],
            "one_order": len(api.c.get("/api/orders", headers=h).json()) == 1}


def x10(api: Api) -> dict[str, bool]:
    checks = {}
    for method, path in [("get", "/api/cart"), ("get", "/api/orders"), ("post", "/api/chat"), ("get", "/api/preferences"), ("post", "/api/checkout/quote")]:
        r = getattr(api.c, method)(path, **({"json": {"message": "hi"}} if method == "post" else {}))
        checks[f"{method}_{path}_401"] = r.status_code == 401
    return checks


def x11(api: Api) -> dict[str, bool]:
    alice, mallory = api.user(), api.user()
    aid = api.propose(alice)["actions"][0]["id"]
    r = api.c.post(f"/api/actions/{aid}/approve", headers=mallory)
    return {"404_for_other_user": r.status_code == 404, "alice_cart_empty": api.cart_count(alice) == 0, "mallory_cart_empty": api.cart_count(mallory) == 0}


def x12(api: Api) -> dict[str, bool]:
    alice, mallory = api.user(), api.user()
    conv = api.propose(alice)["first"]["conversation_id"]
    return {"read_404": api.c.get(f"/api/conversations/{conv}", headers=mallory).status_code == 404,
            "post_404": api.c.post("/api/chat", headers=mallory, json={"message": "hi", "conversation_id": conv}).status_code == 404,
            "delete_404": api.c.delete(f"/api/conversations/{conv}", headers=mallory).status_code == 404}


def x13(api: Api) -> dict[str, bool]:
    alice, mallory = api.user(), api.user()
    order = api.confirm(alice, api.quote(alice)["quote_id"]).json()
    q2 = api.quote(alice, 3)
    api.quote(mallory, 3)
    return {"order_404": api.c.get(f"/api/orders/{order['id']}", headers=mallory).status_code == 404,
            "quote_404": api.confirm(mallory, q2["quote_id"]).status_code == 404,
            "orders_empty": api.c.get("/api/orders", headers=mallory).json() == []}


def x14(api: Api) -> dict[str, bool]:
    import jwt
    from datetime import UTC, datetime, timedelta

    from app.core.config import get_settings

    now = datetime.now(UTC)
    expired = jwt.encode({"sub": "1", "iat": now - timedelta(hours=2), "exp": now - timedelta(hours=1), "typ": "access"}, get_settings().jwt_secret, "HS256")
    forged = jwt.encode({"sub": "1", "iat": now, "exp": now + timedelta(hours=1), "typ": "access"}, "not-the-real-secret-" + "z" * 30, "HS256")
    return {name: api.c.get("/api/cart", headers={"Authorization": f"Bearer {tok}"}).status_code == 401
            for name, tok in {"expired": expired, "forged": forged, "garbage": "abc.def.ghi"}.items()}


def x15(api: Api) -> dict[str, bool]:
    h = api.user()
    q = api.quote(h)
    r = api.c.post("/api/checkout/confirm", headers=h, json={"quote_id": q["quote_id"], "confirm": False, "idempotency_key": "eval-key-xyz"})
    return {"422": r.status_code == 422, "no_order": api.c.get("/api/orders", headers=h).json() == []}


API_IMPL: dict[str, Callable[[Api], dict[str, bool]]] = {
    "X01": x01, "X02": x02, "X03": x03, "X04": x04, "X05": x05, "X06": x06, "X07": x07, "X08": x08,
    "X09": x09, "X10": x10, "X11": x11, "X12": x12, "X13": x13, "X14": x14, "X15": x15,
}


def eval_api_scenario(sc: dict[str, Any]) -> dict[str, Any]:
    t0 = time.perf_counter()
    api = Api()
    try:
        checks = API_IMPL[sc["id"]](api)
        error = None
    except Exception as exc:  # noqa: BLE001 - a crash is a failed scenario, not a crashed run
        checks, error = {"no_exception": False}, f"{type(exc).__name__}: {exc}"
    finally:
        api.close()
    unapproved = sc["id"] in {"X01", "X11"} and checks.get("cart_empty_before", checks.get("alice_cart_empty", True)) is False
    return {"id": sc["id"], "category": sc["category"], "description": sc["description"], "kind": "api",
            "passed": all(checks.values()) and error is None, "checks": checks, "error": error,
            "unapproved_execution": unapproved, "ms": round((time.perf_counter() - t0) * 1000, 1)}


# ============================================================================ metrics & report


def summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    agent = [r for r in results if r["kind"] == "agent"]
    gt_total = sum(r["gt_total"] for r in agent)
    top1 = [r["top1_hit"] for r in agent if r["top1_hit"] is not None]
    top3 = [r["top3_hit"] for r in agent if r["top3_hit"] is not None]
    actions = sum(r["actions_proposed"] for r in agent)
    invalid = sum(len(r["invalid_actions"]) for r in agent) + sum(1 for r in results if r.get("unapproved_execution"))
    tools = sum(r["tool_calls"] for r in agent)
    cards = sum(r["cards_checked"] for r in agent)
    by_cat: dict[str, list[bool]] = defaultdict(list)
    for r in results:
        by_cat[r["category"]].append(r["passed"])
    return {
        "scenarios": len(results),
        "passed": sum(r["passed"] for r in results),
        "scenario_pass_rate": round(sum(r["passed"] for r in results) / len(results), 4),
        "constraint_satisfaction_rate": round(sum(r["gt_satisfied"] for r in agent) / gt_total, 4) if gt_total else None,
        "constraint_checks": gt_total,
        "top1_accuracy": round(sum(top1) / len(top1), 4) if top1 else None,
        "top1_cases": len(top1),
        "top3_accuracy": round(sum(top3) / len(top3), 4) if top3 else None,
        "top3_cases": len(top3),
        "actions_proposed": actions,
        "invalid_action_rate": round(invalid / max(1, actions + len([r for r in results if r["kind"] == "api"])), 4),
        "invalid_actions": invalid,
        "tool_call_success_rate": round(sum(r["tool_calls_ok"] for r in agent) / tools, 4) if tools else None,
        "tool_calls": tools,
        "grounded_card_rate": round(sum(r["cards_grounded"] for r in agent) / cards, 4) if cards else None,
        "cards_checked": cards,
        "by_category": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_cat.items())},
    }


def markdown(summary: dict[str, Any], results: list[dict[str, Any]]) -> str:
    lines = ["# AgentCart evaluation report", "",
             f"Scenarios: **{summary['passed']}/{summary['scenarios']} passed** ({summary['scenario_pass_rate']:.1%})", "",
             "| Metric | Value | Basis |", "|---|---|---|",
             f"| Constraint satisfaction | {summary['constraint_satisfaction_rate']:.1%} | {summary['constraint_checks']} recommended products checked against independent ground truth |",
             f"| Top-1 accuracy | {summary['top1_accuracy']:.1%} | {summary['top1_cases']} scenarios with a known best answer |",
             f"| Top-3 accuracy | {summary['top3_accuracy']:.1%} | {summary['top3_cases']} scenarios with expected top-3 candidates |",
             f"| Invalid-action rate | {summary['invalid_action_rate']:.1%} | {summary['invalid_actions']} invalid of {summary['actions_proposed']} agent proposals + API approval checks |",
             f"| Tool-call success | {summary['tool_call_success_rate']:.1%} | {summary['tool_calls']} tool calls |",
             f"| Grounded product cards | {summary['grounded_card_rate']:.1%} | {summary['cards_checked']} cards (price & rating equal catalogue) |",
             "", "## By category", "", "| Category | Passed |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in summary["by_category"].items()]
    lines += ["", "## Scenarios", "", "| ID | Category | Description | Result | Failed checks |", "|---|---|---|---|---|"]
    for r in results:
        failed = ", ".join(k for k, v in r["checks"].items() if not v) or ""
        if r.get("error"):
            failed += f" ({r['error']})"
        lines.append(f"| {r['id']} | {r['category']} | {r['description']} | {'✅' if r['passed'] else '❌'} | {failed} |")
    return "\n".join(lines) + "\n"


def run(report_dir: Path | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    import logging

    logging.disable(logging.WARNING)
    spec = json.loads((ROOT / "evaluation" / "scenarios.json").read_text(encoding="utf-8"))
    results = [eval_agent_scenario(sc) for sc in spec["agent_scenarios"]]
    results += [eval_api_scenario(sc) for sc in spec["api_scenarios"]]
    summary = summarise(results)
    if report_dir:
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "latest.json").write_text(json.dumps({"summary": summary, "results": results}, indent=2, default=str), encoding="utf-8")
        (report_dir / "latest.md").write_text(markdown(summary, results), encoding="utf-8")
    return summary, results


def threshold_failures(summary: dict[str, Any]) -> list[str]:
    out = []
    for key, limit in THRESHOLDS.items():
        value = summary[key]
        ok = value <= limit if key == "invalid_action_rate" else value >= limit
        if not ok:
            out.append(f"{key}={value} (threshold {limit})")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", default=str(ROOT / "evaluation" / "reports"))
    parser.add_argument("--strict", action="store_true", help="exit non-zero if any threshold fails")
    args = parser.parse_args()
    summary, results = run(Path(args.report_dir))
    for r in results:
        if not r["passed"]:
            failed = [k for k, v in r["checks"].items() if not v]
            print(f"FAIL {r['id']} {r['description']}: {failed} {r.get('error') or ''} top3={r.get('top3')}")  # noqa: T201
    print(json.dumps(summary, indent=2))  # noqa: T201
    problems = threshold_failures(summary)
    if problems:
        print("THRESHOLDS NOT MET: " + "; ".join(problems))  # noqa: T201
    return 1 if (args.strict and problems) else 0


if __name__ == "__main__":
    raise SystemExit(main())
