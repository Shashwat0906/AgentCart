"""Migration 0004 (Phase 3 S4): agent actions linked to the task step that proposed them.

Runs on a temporary SQLite file and, when MIGRATION_TEST_DATABASE_URL points at a scratch PostgreSQL
database (never production), on PostgreSQL too.
"""

from __future__ import annotations

import os
import pathlib

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from tests.test_agent_loop import _alembic, _migration_urls

BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _engine(url: str) -> Engine:
    eng = create_engine(url)
    if url.startswith("sqlite"):

        @event.listens_for(eng, "connect")
        def _fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
            dbapi_conn.execute("PRAGMA foreign_keys=ON")  # as the application does

    return eng


def _seed_0003(eng: Engine) -> None:
    """Rows as they exist before 0004: a chat proposal, a task and one of its steps."""
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO users (email, display_name, password_hash, created_at) "
                "VALUES ('m4@example.com', 'M', 'x', CURRENT_TIMESTAMP)"
            )
        )
        uid = c.execute(text("SELECT id FROM users WHERE email = 'm4@example.com'")).scalar_one()
        c.execute(
            text(
                "INSERT INTO conversations (id, user_id, title, context, created_at, updated_at) "
                "VALUES ('c-1', :u, 't', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"u": uid},
        )
        c.execute(
            text(
                "INSERT INTO agent_actions (id, user_id, conversation_id, kind, payload, summary, status, result, "
                "created_at, expires_at) VALUES ('chat-action', :u, 'c-1', 'add_to_cart', "
                "'{\"product_id\": 1, \"quantity\": 1, \"expected_price_inr\": 999}', 'Add 1 × X', 'pending', '{}', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"u": uid},
        )
        c.execute(
            text(
                "INSERT INTO agent_tasks (id, user_id, kind, goal, input, status, progress, current_step, "
                "total_steps, cancel_requested, attempt, created_at, updated_at) VALUES ('t-1', :u, "
                "'shopping_agent', 'g', '{}', 'succeeded', 100, 1, 1, false, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"u": uid},
        )
        c.execute(
            text(
                "INSERT INTO agent_task_steps (task_id, position, name, tool, status, attempts, max_attempts) "
                "VALUES ('t-1', 0, 'propose_add_to_cart', 'propose_add_to_cart', 'succeeded', 1, 3)"
            )
        )


def _insert_task_action(eng: Engine, action_id: str, step_id: int) -> None:
    with eng.begin() as c:
        uid = c.execute(text("SELECT id FROM users WHERE email = 'm4@example.com'")).scalar_one()
        c.execute(
            text(
                "INSERT INTO agent_actions (id, user_id, task_id, task_step_id, kind, payload, summary, status, "
                "result, created_at, expires_at) VALUES (:id, :u, 't-1', :s, 'add_to_cart', '{}', 'Add', 'pending', "
                "'{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"id": action_id, "u": uid, "s": step_id},
        )


@pytest.mark.parametrize("which", ["sqlite", "postgresql"])
def test_migration_0004_preserves_data_enforces_one_action_per_step_and_rolls_back(
    tmp_path: pathlib.Path, which: str
) -> None:
    urls = [u for u in _migration_urls(tmp_path) if u.startswith(which)]
    if not urls:
        pytest.skip("set MIGRATION_TEST_DATABASE_URL to a scratch PostgreSQL database to run this")
    url = urls[0]
    env = {**os.environ, "DATABASE_URL": url, "ENVIRONMENT": "test"}
    if which == "postgresql":
        _alembic(BACKEND, env, "downgrade", "base")  # the scratch database starts empty
    _alembic(BACKEND, env, "upgrade", "0003")
    eng = _engine(url)
    _seed_0003(eng)

    _alembic(BACKEND, env, "upgrade", "head")
    assert "0004 (head)" in _alembic(BACKEND, env, "current")
    assert "No new upgrade operations detected" in _alembic(BACKEND, env, "check")  # migration == models
    insp = inspect(eng)
    cols = {c["name"]: c for c in insp.get_columns("agent_actions")}
    assert cols["task_id"]["nullable"] and cols["task_step_id"]["nullable"]
    assert {u["name"] for u in insp.get_unique_constraints("agent_actions")} >= {"uq_agent_actions_task_step_id"}
    assert {fk["name"] for fk in insp.get_foreign_keys("agent_actions")} >= {
        "fk_agent_actions_task_id_agent_tasks",
        "fk_agent_actions_task_step_id_agent_task_steps",
    }
    assert "ix_agent_actions_task_id" in {i["name"] for i in insp.get_indexes("agent_actions")}

    with eng.connect() as c:  # existing rows survive unchanged, unlinked
        row = c.execute(
            text("SELECT status, conversation_id, task_id, task_step_id FROM agent_actions WHERE id = 'chat-action'")
        ).one()
        assert tuple(row) == ("pending", "c-1", None, None)
        step_id = c.execute(text("SELECT id FROM agent_task_steps WHERE task_id = 't-1'")).scalar_one()

    _insert_task_action(eng, "task-action", step_id)
    with pytest.raises(IntegrityError):  # one action per step
        _insert_task_action(eng, "duplicate", step_id)
    with eng.begin() as c:  # NULL step links don't collide (chat actions)
        c.execute(text("UPDATE agent_actions SET task_step_id = NULL WHERE id = 'chat-action'"))

    _alembic(BACKEND, env, "downgrade", "0003")
    insp = inspect(eng)
    assert {"task_id", "task_step_id"}.isdisjoint({c["name"] for c in insp.get_columns("agent_actions")})
    with eng.connect() as c:  # the downgrade drops only the link, never an action
        assert c.execute(text("SELECT id, status FROM agent_actions ORDER BY id")).all() == [
            ("chat-action", "pending"),
            ("task-action", "pending"),
        ]

    _alembic(BACKEND, env, "upgrade", "head")
    assert "No new upgrade operations detected" in _alembic(BACKEND, env, "check")
    _insert_task_action(eng, "relinked", step_id)
    with eng.begin() as c:  # deleting a task keeps the user's action and clears the link
        c.execute(text("DELETE FROM agent_tasks WHERE id = 't-1'"))
    with eng.connect() as c:
        assert tuple(
            c.execute(text("SELECT task_id, task_step_id FROM agent_actions WHERE id = 'relinked'")).one()
        ) == (
            None,
            None,
        )
    eng.dispose()
