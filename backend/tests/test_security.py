"""Login cookies, session revocation and rate limits.

CORS is covered in test_app_factory.py (test_cors_allows_only_the_frontend_origin, test_cors_preflight).
"""

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import pytest

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import User


def login(client, email, password):
    return client.post("/api/login", json={"email": email, "password": password})


def set_cookies(response):
    """{name: [attribute, ...]} for every Set-Cookie header of the response."""
    cookies = {}
    for header in response.headers.getlist("Set-Cookie"):
        name_value, *attributes = [part.strip() for part in header.split(";")]
        cookies[name_value.split("=", 1)[0]] = attributes
    return cookies


def attribute(attributes, name):
    for item in attributes:
        key, _, value = item.partition("=")
        if key.lower() == name.lower():
            return value
    return None


def client_with_cookie(app, name, value):
    client = app.test_client()
    client.set_cookie(name, value)
    return client


# --- cookies -------------------------------------------------------------------------------------


def test_login_cookies_are_hardened_and_last_30_days(client, user, user_password):
    response = login(client, user.email, user_password)
    cookies = set_cookies(response)

    now = datetime.now(timezone.utc)
    for name in ("session", "remember_token"):
        attributes = cookies[name]
        assert "HttpOnly" in attributes
        assert "Secure" in attributes
        assert attribute(attributes, "SameSite") == "Lax"
        expires = parsedate_to_datetime(attribute(attributes, "Expires"))
        assert now + timedelta(days=29) < expires <= now + timedelta(days=30, minutes=1)


# --- revocation ----------------------------------------------------------------------------------


@pytest.mark.parametrize("cookie", ["remember_token", "session"])
def test_captured_cookie_stops_working_after_logout(app, client, user, user_password, cookie):
    login(client, user.email, user_password)
    captured = client.get_cookie(cookie).value
    assert client_with_cookie(app, cookie, captured).get("/api/check-auth").status_code == 200

    client.post("/api/logout")

    assert client_with_cookie(app, cookie, captured).get("/api/check-auth").status_code == 401


def test_captured_remember_cookie_stops_working_after_a_password_reset(app, client, user, user_password,
                                                                       mock_requests):
    login(client, user.email, user_password)
    captured = client.get_cookie("remember_token").value

    anonymous = app.test_client()
    anonymous.post("/api/forgot-password", json={"email": user.email})
    text = mock_requests.post.call_args.kwargs["data"]["text"]
    token = text.split("/reset-password/", 1)[1].split()[0]
    assert anonymous.post("/api/reset-password", json={"token": token, "password": "new-password"}).status_code == 200

    assert client_with_cookie(app, "remember_token", captured).get("/api/check-auth").status_code == 401
    assert client.get("/api/check-auth").status_code == 401  # the session that was open is over too


@pytest.mark.parametrize("login_id", ["{id}", "{id}:", "{id}:wrong-token", "abc:def", ":token"])
def test_login_ids_without_the_current_session_token_are_anonymous(app, user, login_id):
    # "{id}" is the format of the cookies issued before session tokens existed.
    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = login_id.format(id=user.id)

    response = client.get("/api/check-auth")

    assert response.status_code == 401
    assert response.get_json() == {"authenticated": False}


def test_deleted_user_cookie_is_anonymous(app, logged_in_client, user):
    with app.app_context():
        db.session.delete(db.session.get(User, user.id))
        db.session.commit()
    assert logged_in_client.get("/api/check-auth").status_code == 401


# --- rate limits ---------------------------------------------------------------------------------


class RateLimitedConfig(TestConfig):
    RATELIMIT_ENABLED = True


@pytest.fixture
def limited_app():
    """An app with the rate limiter on (fresh in-memory counters)."""
    app = create_app(RateLimitedConfig)
    with app.app_context():
        db.create_all()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def from_ip(ip):
    return {"environ_base": {"REMOTE_ADDR": ip}}


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/login", {"email": "nobody@example.com", "password": "wrong password"}),
        ("/api/register", {"email": "bad", "password": "x"}),
        ("/api/authorize/google", {}),
    ],
)
def test_ten_requests_per_minute_per_ip(limited_app, path, body):
    client = limited_app.test_client()
    statuses = [client.post(path, json=body, **from_ip("198.51.100.1")).status_code for _ in range(11)]

    assert 429 not in statuses[:10]
    assert statuses[10] == 429
    # Another address still gets through.
    assert client.post(path, json=body, **from_ip("198.51.100.2")).status_code != 429


def test_login_limit_answers_json(limited_app):
    client = limited_app.test_client()
    for _ in range(10):
        login(client, "nobody@example.com", "wrong password")

    response = login(client, "nobody@example.com", "wrong password")

    assert response.status_code == 429
    assert response.get_json()["error"] == "Too Many Requests"


def test_forgot_password_three_per_hour_per_email(limited_app, mock_requests):
    client = limited_app.test_client()
    emails = ["victim@example.com", "Victim@example.com", " victim@example.com", "victim@example.com"]

    # Different addresses, same (case-insensitive) email.
    statuses = [
        client.post("/api/forgot-password", json={"email": email}, **from_ip(f"198.51.100.{i}")).status_code
        for i, email in enumerate(emails, start=1)
    ]

    assert statuses == [200, 200, 200, 429]
    other = client.post("/api/forgot-password", json={"email": "other@example.com"}, **from_ip("198.51.100.9"))
    assert other.status_code == 200


def test_forgot_password_three_per_hour_per_ip(limited_app, mock_requests):
    client = limited_app.test_client()

    statuses = [
        client.post("/api/forgot-password", json={"email": f"person{i}@example.com"},
                    **from_ip("198.51.100.1")).status_code
        for i in range(4)
    ]

    assert statuses == [200, 200, 200, 429]
