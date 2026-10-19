"""Test fixtures.

By default tests use an in-memory SQLite database (fast, no services needed). Set
TEST_DATABASE_URL to a PostgreSQL URL to run the same suite against Postgres (CI does this).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

os.environ["ENVIRONMENT"] = "test"
os.environ["RATE_LIMIT_ENABLED"] = "false"
os.environ["LLM_PROVIDER"] = "mock"
os.environ.setdefault("JWT_SECRET", "test-secret-" + "x" * 40)
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_provider
from app.api.routes_tasks import get_task_runner
from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.db.session import get_db
from app.main import app
from app.models import Base, Product
from app.providers import MockProvider
from app.seed import seed_catalog
from app.services.tasks import TaskRunner

TEST_DB_URL = os.environ["DATABASE_URL"]
IS_SQLITE = TEST_DB_URL.startswith("sqlite")


def _make_engine() -> Engine:
    if IS_SQLITE:
        eng = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False}, poolclass=StaticPool)

        @event.listens_for(eng, "connect")
        def _fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

        return eng
    return create_engine(TEST_DB_URL, pool_pre_ping=True)


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    eng = _make_engine()
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture()
def SessionLocal(engine: Engine) -> Iterator[sessionmaker[Session]]:
    # Fresh data for every test: wipe all tables and re-seed the catalogue.
    with engine.begin() as conn:
        if IS_SQLITE:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())
        else:
            names = ", ".join(t.name for t in Base.metadata.sorted_tables)
            conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with maker() as s:
        seed_catalog(s)
    yield maker


@pytest.fixture()
def db(SessionLocal: sessionmaker[Session]) -> Iterator[Session]:
    with SessionLocal() as s:
        yield s


@pytest.fixture()
def client(SessionLocal: sessionmaker[Session]) -> Iterator[TestClient]:
    def _get_db() -> Iterator[Session]:
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_provider] = lambda: MockProvider()
    # Background task runs use the test database and never sleep between retries.
    app.dependency_overrides[get_task_runner] = lambda: TaskRunner(SessionLocal, get_settings(), sleep=lambda _s: None)
    limiter.reset()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def register(client: TestClient, email: str | None = None, password: str = "Passw0rd!x") -> dict[str, str]:
    email = email or f"user-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/api/auth/register", json={"email": email, "password": password, "display_name": "Test User"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def auth(client: TestClient) -> dict[str, str]:
    return register(client)


def product_by_sku(db: Session, sku: str) -> Product:
    from sqlalchemy import select

    p = db.scalar(select(Product).where(Product.sku == sku))
    assert p is not None
    return p
