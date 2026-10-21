"""Approval is idempotent under concurrent requests (Phase 3 S4).

Two requests deciding the same action at once (a double click, a retry, two tabs) must change the cart at
most once. Needs real row locks, so it runs on PostgreSQL (CI's backend job); the SQLite test engine shares a
single connection between threads.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.models import AgentAction, CartItem, Product
from app.services import actions as action_service
from app.services import cart as cart_service
from tests.conftest import IS_SQLITE, register
from tests.test_tasks import uid

pytestmark = pytest.mark.skipif(IS_SQLITE, reason="needs PostgreSQL row locks (set TEST_DATABASE_URL)")


def _pending_add(SessionLocal: sessionmaker[Session], user_id: int) -> tuple[str, int]:
    with SessionLocal() as s:
        product = s.scalars(select(Product).where(Product.stock >= 5).order_by(Product.id)).first()
        assert product is not None
        payload = {"product_id": product.id, "quantity": 1, "expected_price_inr": product.price_inr}
        [action] = action_service.persist_actions(
            s, user_id, None, [{"kind": "add_to_cart", "payload": payload, "summary": "Add 1"}], get_settings()
        )
        s.commit()
        return action.id, product.id


@pytest.mark.parametrize("second", ["approve", "reject"])
def test_concurrent_decisions_change_the_cart_at_most_once(
    client: TestClient, SessionLocal: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, second: str
) -> None:
    user_id = uid(client, register(client))
    action_id, product_id = _pending_add(SessionLocal, user_id)
    inside, release = threading.Event(), threading.Event()
    real_add = cart_service.add_item

    def paused_add(*args: Any, **kwargs: Any) -> Any:
        if threading.current_thread().name == "first":
            inside.set()  # the first approval is mid-way through changing the cart …
            release.wait(10)  # … and stays there until the second request has arrived
        return real_add(*args, **kwargs)

    monkeypatch.setattr(cart_service, "add_item", paused_add)
    outcome: dict[str, str] = {}

    def decide(name: str, op: str) -> None:
        with SessionLocal() as s:
            try:
                if op == "approve":
                    action_service.approve(s, user_id, action_id, get_settings())
                else:
                    action_service.reject(s, user_id, action_id)
                outcome[name] = "done"
            except action_service.ActionGone:
                outcome[name] = "gone"

    first = threading.Thread(target=decide, args=("first", "approve"), name="first")
    first.start()
    assert inside.wait(10)
    late = threading.Thread(target=decide, args=("second", second), name="second")
    late.start()
    time.sleep(0.5)  # the second request is now waiting on a lock
    release.set()
    first.join(10)
    late.join(10)

    assert outcome == {"first": "done", "second": "gone"}
    with SessionLocal() as s:
        item = s.scalar(select(CartItem).where(CartItem.product_id == product_id))
        assert item is not None and item.quantity == 1  # added exactly once
        action = s.get(AgentAction, action_id)
        assert action is not None and action.status == "executed"
