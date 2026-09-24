"""GET /users/search — who is findable, and what the searcher is told about them.

The route is declared above /users/{user_id}; the first test here is what catches
it being moved below, since "search" would then fail to parse as an int.
"""

import datetime
from typing import NamedTuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registers every model on Base before create_all
from app.db.session import Base, get_db
from app.main import app
from app.models.cat import Cat
from app.models.sighting import Sighting
from app.models.user import User
from app.routers.users import MAX_SEARCH_RESULTS
from app.services.auth_service import create_access_token, hash_password
from app.services.demo_seed import DEMO_EMAIL


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    c = TestClient(app)
    c.Session = Session  # type: ignore[attr-defined]
    try:
        yield c
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


class U(NamedTuple):
    id: int


def _make_user(session, email, display_name="Spotter", **overrides):
    fields = {
        "email": email,
        "hashed_password": hash_password("hunter2pw"),
        "display_name": display_name,
        "email_verified": True,
        "is_active": True,
    }
    fields.update(overrides)
    user = User(**fields)
    session.add(user)
    session.commit()
    session.refresh(user)
    return U(user.id)


def _auth(user):
    return {"Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}"}


def _search(client, user, q):
    return client.get(f"/users/search?q={q}", headers=_auth(user)).json()


def test_search_is_not_captured_by_the_user_id_route(client):
    session = client.Session()
    try:
        a = _make_user(session, "a@example.com", "Ada")
    finally:
        session.close()
    res = client.get("/users/search?q=ad", headers=_auth(a))
    assert res.status_code == 200


def test_partial_case_insensitive_match(client):
    session = client.Session()
    try:
        a = _make_user(session, "a@example.com", "Ada")
        marta = _make_user(session, "m@example.com", "Marta Chen")
    finally:
        session.close()

    for q in ("mar", "MAR", "Chen", "ta ch"):
        assert [r["id"] for r in _search(client, a, q)] == [marta.id], q


def test_short_queries_return_nothing_rather_than_erroring(client):
    session = client.Session()
    try:
        a = _make_user(session, "a@example.com", "Ada")
        _make_user(session, "m@example.com", "Marta")
    finally:
        session.close()

    for q in ("", "m"):
        res = client.get(f"/users/search?q={q}", headers=_auth(a))
        assert res.status_code == 200
        assert res.json() == []


@pytest.mark.parametrize(
    "overrides,email",
    [
        ({"is_active": False}, "x1@example.com"),
        ({"banned_at": datetime.datetime(2026, 1, 1)}, "x2@example.com"),
        ({"email_verified": False}, "x3@example.com"),
        ({"display_name": None}, "x4@example.com"),
        ({}, DEMO_EMAIL),
    ],
)
def test_unfindable_accounts_are_excluded(client, overrides, email):
    session = client.Session()
    try:
        a = _make_user(session, "a@example.com", "Ada")
        _make_user(session, email, **{"display_name": "Marta", **overrides})
    finally:
        session.close()

    assert _search(client, a, "mar") == []


def test_the_searcher_is_excluded_from_their_own_results(client):
    session = client.Session()
    try:
        marta = _make_user(session, "m@example.com", "Marta")
        other = _make_user(session, "o@example.com", "Martin")
    finally:
        session.close()

    assert [r["id"] for r in _search(client, marta, "mar")] == [other.id]


@pytest.mark.parametrize(
    "arrange,expected",
    [
        (None, "none"),
        ("outgoing", "outgoing"),
        ("incoming", "incoming"),
        ("friends", "friends"),
    ],
)
def test_friend_status_per_result(client, arrange, expected):
    session = client.Session()
    try:
        a = _make_user(session, "a@example.com", "Ada")
        marta = _make_user(session, "m@example.com", "Marta")
    finally:
        session.close()

    if arrange == "outgoing":
        client.post("/friends/requests", json={"user_id": marta.id}, headers=_auth(a))
    elif arrange == "incoming":
        client.post("/friends/requests", json={"user_id": a.id}, headers=_auth(marta))
    elif arrange == "friends":
        client.post("/friends/requests", json={"user_id": marta.id}, headers=_auth(a))
        client.post(f"/friends/requests/{a.id}/accept", headers=_auth(marta))

    assert _search(client, a, "mar")[0]["friend_status"] == expected


def test_a_declined_request_reads_as_none(client):
    session = client.Session()
    try:
        a = _make_user(session, "a@example.com", "Ada")
        marta = _make_user(session, "m@example.com", "Marta")
    finally:
        session.close()

    client.post("/friends/requests", json={"user_id": marta.id}, headers=_auth(a))
    client.post(f"/friends/requests/{a.id}/decline", headers=_auth(marta))

    assert _search(client, a, "mar")[0]["friend_status"] == "none"


def test_cats_spotted_is_reported(client):
    session = client.Session()
    try:
        a = _make_user(session, "a@example.com", "Ada")
        marta = _make_user(session, "m@example.com", "Marta")
        for name in ("One", "Two"):
            cat = Cat(name=name)
            session.add(cat)
            session.commit()
            session.refresh(cat)
            session.add(
                Sighting(
                    user_id=marta.id,
                    cat_id=cat.id,
                    photo_path="uploads/x.jpg",
                    latitude=51.5,
                    longitude=-0.12,
                )
            )
        session.commit()
    finally:
        session.close()

    assert _search(client, a, "mar")[0]["cats_spotted"] == 2


def test_results_are_capped(client):
    session = client.Session()
    try:
        a = _make_user(session, "a@example.com", "Ada")
        for i in range(MAX_SEARCH_RESULTS + 5):
            _make_user(session, f"m{i}@example.com", f"Marta {i}")
    finally:
        session.close()

    res = client.get(
        f"/users/search?q=mar&limit={MAX_SEARCH_RESULTS + 5}", headers=_auth(a)
    ).json()
    assert len(res) == MAX_SEARCH_RESULTS


def test_search_works_without_a_token(client):
    """Anonymous callers may search; every result just reads as "none"."""
    session = client.Session()
    try:
        _make_user(session, "m@example.com", "Marta")
    finally:
        session.close()

    res = client.get("/users/search?q=mar")
    assert res.status_code == 200
    assert res.json()[0]["friend_status"] == "none"
