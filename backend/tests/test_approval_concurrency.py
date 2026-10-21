"""Approval is idempotent under concurrent requests (Phase 3 S4).

Two requests deciding the same action at once (a double click, a retry, two tabs) must change the cart at
most once. Needs real row locks, so it runs on PostgreSQL (CI's backend job); the SQLite test engine shares a
single connection between threads.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import Counter
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.errors import ConflictError
from app.models import AgentAction, Cart, CartItem, Conversation, Product
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


def _conversation(SessionLocal: sessionmaker[Session], user_id: int) -> str:
    with SessionLocal() as s:
        conv = Conversation(id=str(uuid.uuid4()), user_id=user_id, title="t", context={})
        s.add(conv)
        s.commit()
        return conv.id


def test_a_new_chat_proposal_never_relabels_an_action_that_is_being_approved(
    client: TestClient, SessionLocal: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new chat message supersedes the conversation's older *pending* proposals. If one of them is being
    approved at that moment, it must end up "executed" (the cart changed), not "superseded"."""
    user_id = uid(client, register(client))
    action_id, product_id = _pending_add(SessionLocal, user_id)
    conv_id = _conversation(SessionLocal, user_id)
    with SessionLocal() as s:
        row = s.get(AgentAction, action_id)
        assert row is not None
        row.conversation_id = conv_id
        s.commit()
    inside, release = threading.Event(), threading.Event()
    real_add = cart_service.add_item

    def paused_add(*args: Any, **kwargs: Any) -> Any:
        inside.set()
        release.wait(10)
        return real_add(*args, **kwargs)

    monkeypatch.setattr(cart_service, "add_item", paused_add)
    errors: list[BaseException] = []

    def approve() -> None:
        with SessionLocal() as s:
            action_service.approve(s, user_id, action_id, get_settings())

    def new_chat_proposal() -> None:
        try:
            with SessionLocal() as s:
                payload = {"product_id": product_id, "quantity": 1, "expected_price_inr": 1}
                action_service.persist_actions(
                    s,
                    user_id,
                    conv_id,
                    [{"kind": "add_to_cart", "payload": payload, "summary": "newer"}],
                    get_settings(),
                )
                s.commit()
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=approve)
    first.start()
    assert inside.wait(10)
    chat = threading.Thread(target=new_chat_proposal)
    chat.start()
    time.sleep(0.5)  # the chat request is now waiting on the action's row lock
    release.set()
    first.join(10)
    chat.join(10)
    assert errors == []
    with SessionLocal() as s:
        assert s.get(AgentAction, action_id).status == "executed"  # type: ignore[union-attr]
        statuses = sorted(
            a.status for a in s.scalars(select(AgentAction).where(AgentAction.conversation_id == conv_id))
        )
        assert statuses == ["executed", "pending"]  # the newer proposal is the only pending one
        item = s.scalar(select(CartItem).where(CartItem.product_id == product_id))
        assert item is not None and item.quantity == 1


def test_mixed_concurrent_decisions_never_deadlock_or_lose_a_cart_change(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    """Approvals, rejections, new chat proposals and direct cart adds race on the same products. Lock order
    (action, then product, then cart rows) must never deadlock, and every executed action is in the cart."""
    user_id = uid(client, register(client))
    conv_id = _conversation(SessionLocal, user_id)
    with SessionLocal() as s:
        products = list(s.scalars(select(Product).where(Product.stock >= 50).order_by(Product.id).limit(2)))
        assert len(products) == 2
        pids = [p.id for p in products]
        ids = []
        for n in range(12):
            p = products[n % 2]
            payload = {"product_id": p.id, "quantity": 1, "expected_price_inr": p.price_inr}
            [a] = action_service.persist_actions(
                s, user_id, None, [{"kind": "add_to_cart", "payload": payload, "summary": f"a{n}"}], get_settings()
            )
            a.conversation_id = conv_id
            ids.append(a.id)
        s.commit()
    unexpected: list[BaseException] = []
    direct_adds = Counter[int]()
    lock = threading.Lock()

    def guard(fn: Any) -> Any:
        def run(*args: Any) -> None:
            try:
                with SessionLocal() as s:
                    fn(s, *args)
            except (action_service.ActionGone, ConflictError):
                pass  # decided by someone else / cart refused: expected outcomes
            except BaseException as exc:
                unexpected.append(exc)

        return run

    def approve(s: Session, aid: str) -> None:
        action_service.approve(s, user_id, aid, get_settings())

    def reject(s: Session, aid: str) -> None:
        action_service.reject(s, user_id, aid)

    def chat(s: Session) -> None:
        payload = {"product_id": pids[0], "quantity": 1, "expected_price_inr": 1}
        action_service.persist_actions(
            s, user_id, conv_id, [{"kind": "add_to_cart", "payload": payload, "summary": "chat"}], get_settings()
        )
        s.commit()

    def add(s: Session, pid: int) -> None:
        cart_service.add_item(s, user_id, pid, 1, get_settings())
        with lock:
            direct_adds[pid] += 1

    jobs: list[tuple[Any, tuple[Any, ...]]] = []
    for n, aid in enumerate(ids):
        jobs += [(approve, (aid,)), (approve, (aid,))] + ([(reject, (aid,))] if n % 3 == 0 else [])
    jobs += [(chat, ())] * 2 + [(add, (pids[n % 2],)) for n in range(4)]
    threads = [threading.Thread(target=guard(fn), args=args) for fn, args in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not any(t.is_alive() for t in threads), "a request is stuck"
    assert unexpected == []
    with SessionLocal() as s:
        statuses = {a.id: a.status for a in s.scalars(select(AgentAction).where(AgentAction.id.in_(ids)))}
        assert set(statuses.values()) <= {"executed", "rejected", "superseded"}
        executed = Counter(
            int(a.payload["product_id"])
            for a in s.scalars(select(AgentAction).where(AgentAction.id.in_(ids), AgentAction.status == "executed"))
        )
        cart = {i.product_id: i.quantity for i in s.scalars(select(CartItem).where(CartItem.product_id.in_(pids)))}
        for pid in pids:  # every executed action and direct add is in the cart exactly once
            assert cart.get(pid, 0) == executed[pid] + direct_adds[pid], (pid, cart, executed, direct_adds)


def test_concurrent_first_cart_writes_share_one_cart(client: TestClient, SessionLocal: sessionmaker[Session]) -> None:
    """Two requests creating a user's first cart at once (e.g. two approvals of different products): the
    second waits on the unique user_id, then uses the cart the first created instead of failing."""
    user_id = uid(client, register(client))
    created, release = threading.Event(), threading.Event()
    result: dict[str, Any] = {}

    def first() -> None:
        with SessionLocal() as s:
            result["first"] = cart_service.get_or_create_cart(s, user_id).id  # inserted, not yet committed
            created.set()
            release.wait(10)
            s.commit()

    def second() -> None:
        try:
            with SessionLocal() as s:
                result["second"] = cart_service.get_or_create_cart(s, user_id).id
                s.commit()
        except BaseException as exc:  # surfaced below
            result["error"] = exc

    a = threading.Thread(target=first)
    a.start()
    assert created.wait(10)
    b = threading.Thread(target=second)
    b.start()
    time.sleep(0.5)  # the second insert is now waiting on the first's uncommitted row
    release.set()
    a.join(10)
    b.join(10)
    assert "error" not in result, result.get("error")
    assert result["first"] == result["second"]


def test_a_failing_approval_relocks_and_never_overwrites_a_concurrent_decision(
    client: TestClient, SessionLocal: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure path rolls back (dropping partial cart changes, and with them the action's row lock) before
    recording "failed". If another approval executes the action in that gap, the failing request must re-lock,
    see "executed" and keep it: the cart changes once, and the action says so."""
    user_id = uid(client, register(client))
    action_id, product_id = _pending_add(SessionLocal, user_id)
    in_gap, release = threading.Event(), threading.Event()
    real_add, real_rollback = cart_service.add_item, Session.rollback
    armed = threading.local()

    def failing_add(*args: Any, **kwargs: Any) -> Any:
        if threading.current_thread().name == "failing":
            armed.on = True  # the next rollback is the failure path's
            raise ConflictError("Only 0 unit(s) available", details={"available": 0})
        return real_add(*args, **kwargs)

    def rollback_then_pause(self: Session) -> None:
        real_rollback(self)
        if getattr(armed, "on", False):
            armed.on = False
            in_gap.set()  # lock released, failure not yet recorded …
            release.wait(10)  # … while another approval goes through

    monkeypatch.setattr(cart_service, "add_item", failing_add)
    monkeypatch.setattr(Session, "rollback", rollback_then_pause)
    outcome: dict[str, str] = {}

    def decide(name: str) -> None:
        with SessionLocal() as s:
            try:
                action_service.approve(s, user_id, action_id, get_settings())
                outcome[name] = "executed"
            except ConflictError as exc:
                outcome[name] = f"conflict:{exc.details.get('reason')}"
            except action_service.ActionGone as exc:
                outcome[name] = f"gone:{exc.details.get('status')}"

    failing = threading.Thread(target=decide, args=("failing",), name="failing")
    failing.start()
    assert in_gap.wait(10)
    other = threading.Thread(target=decide, args=("other",), name="other")
    other.start()
    other.join(10)  # not blocked: the failing request's rollback released the row lock
    assert outcome.get("other") == "executed"
    release.set()
    failing.join(10)

    assert outcome["failing"] == "conflict:cart_rejected"  # its own attempt still reports the problem
    with SessionLocal() as s:
        action = s.get(AgentAction, action_id)
        assert action is not None and action.status == "executed" and "reason" not in action.result
        item = s.scalar(select(CartItem).where(CartItem.product_id == product_id))
        assert item is not None and item.quantity == 1  # exactly one cart mutation
        # lock cleanup: nobody still holds the action's row lock
        s.execute(select(AgentAction).where(AgentAction.id == action_id).with_for_update(nowait=True)).one()
        s.rollback()


def test_a_failed_approval_releases_the_lock_and_leaves_no_partial_cart(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    user_id = uid(client, register(client))
    action_id, product_id = _pending_add(SessionLocal, user_id)
    with SessionLocal() as s:
        product = s.get(Product, product_id)
        assert product is not None
        product.stock = 0
        s.commit()
    with SessionLocal() as s:  # the approving session stays open afterwards
        with pytest.raises(ConflictError):
            action_service.approve(s, user_id, action_id, get_settings())
        with SessionLocal() as other:  # its lock is gone: another session can lock the row without waiting
            other.execute(select(AgentAction).where(AgentAction.id == action_id).with_for_update(nowait=True)).one()
            other.rollback()
    with SessionLocal() as s:
        action = s.get(AgentAction, action_id)
        assert action is not None and action.status == "failed" and action.result["reason"] == "cart_rejected"
        assert s.scalar(select(Cart).where(Cart.user_id == user_id)) is None  # the cart created mid-way was rolled back
        with pytest.raises(action_service.ActionGone):  # and it can't be retried into the cart
            action_service.approve(s, user_id, action_id, get_settings())
