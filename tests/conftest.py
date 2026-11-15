"""App tests run against a REAL PostgreSQL database, set with TEST_DATABASE_URL.

Every table is wiped before each test, so the database name must contain "test".
Without TEST_DATABASE_URL these tests are skipped (the pure maths tests still run).
"""
import os

import pytest

TEST_URL = os.environ.get("TEST_DATABASE_URL", "").strip()


@pytest.fixture
def app():
    if not TEST_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    if "test" not in TEST_URL.rsplit("/", 1)[-1]:
        pytest.fail("TEST_DATABASE_URL must point at a database whose name contains 'test'")
    os.environ["DATABASE_URL"] = TEST_URL
    from app import create_app
    from db import get_db

    application = create_app()
    application.config.update(TESTING=True)
    with get_db() as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE settlements, payments, people, bills, users RESTART IDENTITY CASCADE")
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def register_and_login(client, username="amina", password="long-enough-pw"):
    client.post("/register", data={"username": username, "password": password})
    client.post("/login", data={"username": username, "password": password})


@pytest.fixture
def user(client):
    register_and_login(client)
    return client


@pytest.fixture
def bill_id(user):
    resp = user.post("/bill/new", data={"name": "Naivasha weekend", "currency": "KSH"})
    return int(resp.headers["Location"].rsplit("/", 1)[-1])
