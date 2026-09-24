import json
import re
import time

import google.auth.exceptions
import pytest
import requests
from freezegun import freeze_time
from sqlalchemy.exc import IntegrityError

from app.config import TestConfig
from app.extensions import db
from app.models import User

NEUTRAL_MESSAGE = "If an account exists for that email, a reset link has been sent."


def get_user(app, user_id):
    with app.app_context():
        user = db.session.get(User, user_id)
        if user is not None:
            db.session.expunge(user)
        return user


def find_user(app, email):
    with app.app_context():
        user = User.query.filter_by(email=email).first()
        if user is not None:
            db.session.expunge(user)
        return user


def login(client, email, password):
    return client.post("/api/login", json={"email": email, "password": password})


# --- register ---------------------------------------------------------------------------------


def test_register_creates_a_password_account(app, client, user_password):
    response = client.post("/api/register", json={"email": "  New.User@Example.COM ", "password": user_password})

    assert response.status_code == 201
    assert response.get_json() == {"message": "User registered successfully"}
    user = find_user(app, "new.user@example.com")  # stored trimmed and lowercased
    assert user.password.startswith("pbkdf2:sha256:")
    assert user.check_password(user_password)
    assert user.google_id is None


def test_registration_grants_no_privileges(app, client, user_password):
    # Registration never grants complimentary access or a subscription; only `flask make-admin` sets superuser.
    client.post("/api/register", json={"email": "owner@example.com", "password": user_password})

    user = find_user(app, "owner@example.com")
    assert user.superuser is False
    assert user.subscription_status is None


@pytest.mark.parametrize("email", ["user@example.com", "USER@example.com "])
def test_register_duplicate_email(client, user, user_password, email):
    response = client.post("/api/register", json={"email": email, "password": user_password})
    assert response.status_code == 400
    assert response.get_json() == {"message": "User already exists"}


def test_register_race_returns_409(app, client, user, user_password, monkeypatch):
    # Both requests passed the duplicate check; the unique index rejects the second insert.
    monkeypatch.setattr("app.routes.auth._find_user_by_email", lambda email: None)

    response = client.post("/api/register", json={"email": "user@example.com", "password": user_password})

    assert response.status_code == 409
    with app.app_context():
        assert User.query.count() == 1


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"email": "a@example.com", "password": "12345"}, "Password must be at least 6 characters long."),
        ({"email": "a@example.com", "password": "x" * 129}, "Password must be at most 128 characters long."),
        ({"password": "secret123"}, "All fields are required"),
        ({"email": "a@example.com"}, "All fields are required"),
        ({"email": "", "password": "secret123"}, "All fields are required"),
        ({"email": {"$ne": None}, "password": "secret123"}, "All fields are required"),
        ({"email": "a@example.com", "password": 12345678}, "All fields are required"),
        ({"email": "not-an-email", "password": "secret123"}, "Invalid email address."),
        ({"email": "a@b", "password": "secret123"}, "Invalid email address."),
        ({"email": "a b@example.com", "password": "secret123"}, "Invalid email address."),
        ({"email": "a" * 243 + "@example.com", "password": "secret123"}, "Invalid email address."),
        (None, "All fields are required"),
        ([], "All fields are required"),
    ],
)
def test_register_rejects_invalid_input(app, client, body, message):
    response = client.post("/api/register", json=body)

    assert response.status_code == 400
    assert response.get_json() == {"message": message}
    with app.app_context():
        assert User.query.count() == 0


def test_register_accepts_the_password_length_limits(client):
    assert client.post("/api/register", json={"email": "a@example.com", "password": "x" * 6}).status_code == 201
    assert client.post("/api/register", json={"email": "b@example.com", "password": "x" * 128}).status_code == 201


def test_register_non_json_body_is_a_json_400(client):
    response = client.post("/api/register", data="email=a@example.com", content_type="application/x-www-form-urlencoded")
    assert response.status_code == 400
    assert response.is_json


# --- login / logout / check-auth -----------------------------------------------------------------


def test_login_sets_the_login_cookies(client, user, user_password):
    response = login(client, user.email, user_password)

    assert response.status_code == 200
    assert response.get_json() == {"message": "Login successful"}
    assert client.get_cookie("session") is not None
    assert client.get_cookie("remember_token") is not None
    assert client.get("/api/check-auth").status_code == 200


def test_login_email_is_case_insensitive(client, user, user_password):
    assert login(client, "  User@Example.com", user_password).status_code == 200


@pytest.mark.parametrize(("email", "password"), [("user@example.com", "wrong password"), ("nobody@example.com", "x")])
def test_login_invalid_credentials(client, user, email, password):
    response = login(client, email, password)
    assert response.status_code == 401
    assert response.get_json() == {"message": "Invalid credentials"}


def test_login_google_only_account_is_401_not_500(client, google_user):
    response = login(client, google_user.email, "anything")
    assert response.status_code == 401
    assert response.get_json() == {"message": "Invalid credentials"}


@pytest.mark.parametrize(
    "body",
    [None, [], {}, {"email": "user@example.com"}, {"password": "x"}, {"email": 5, "password": "x"},
     {"email": {"a": 1}, "password": "secret123"}, {"email": "user@example.com", "password": ["x"]},
     {"email": "user@example.com", "password": ""}],
)
def test_login_rejects_malformed_input(client, user, body):
    response = client.post("/api/login", json=body)
    assert response.status_code == 400
    assert response.get_json() == {"message": "Email and password are required"}


def test_check_auth_anonymous(client):
    response = client.get("/api/check-auth")
    assert response.status_code == 401
    assert response.get_json() == {"authenticated": False}


def test_check_auth_logged_in(logged_in_client, user):
    response = logged_in_client.get("/api/check-auth")
    assert response.status_code == 200
    assert response.get_json() == {"authenticated": True, "user": {"name": "Test User", "email": user.email}}


def test_user_endpoint_and_logout(app, logged_in_client, user):
    response = logged_in_client.get("/api/user")
    assert response.status_code == 200
    assert response.get_json() == {"email": user.email}

    response = logged_in_client.post("/api/logout")

    assert response.status_code == 200
    assert response.get_json() == {"message": "Logged out successfully"}
    assert logged_in_client.get("/api/check-auth").status_code == 401
    assert get_user(app, user.id).session_token != user.session_token  # every other session ends too


def test_logout_requires_login(client):
    assert client.post("/api/logout").status_code == 401


# --- Google sign-in ------------------------------------------------------------------------------


def google_login(client, credential="google-id-token"):
    return client.post("/api/authorize/google", json={"credential": credential})


@pytest.mark.parametrize("body", [{}, {"credential": ""}, {"credential": 123}, {"token": "access-token"}, None])
def test_google_requires_a_credential(client, mock_google, body):
    response = client.post("/api/authorize/google", json=body)
    assert response.status_code == 400
    mock_google.assert_not_called()


def test_google_verifies_the_id_token_against_our_client_id(client, mock_google):
    assert google_login(client, "the-id-token").status_code == 200

    args = mock_google.call_args.args
    assert args[0] == "the-id-token"
    assert callable(args[1])  # the transport used to fetch Google's certificates
    assert args[2] == TestConfig.GOOGLE_CLIENT_ID


@pytest.mark.parametrize(
    "error",
    [ValueError("Token has wrong audience"), ValueError("Token expired"),
     google.auth.exceptions.GoogleAuthError("Wrong issuer")],
)
def test_google_rejected_token_is_401(app, client, mock_google, error):
    mock_google.side_effect = error

    response = google_login(client)

    assert response.status_code == 401
    assert response.get_json() == {"message": "Failed to authenticate"}
    with app.app_context():
        assert User.query.count() == 0


def test_google_certificate_fetch_failure_is_502(client, mock_google):
    mock_google.side_effect = google.auth.exceptions.TransportError("connection reset")
    assert google_login(client).status_code == 502


@pytest.mark.parametrize("email_verified", [False, None, "true"])
def test_google_unverified_email_is_401(app, client, mock_google, email_verified):
    claims = dict(mock_google.return_value)
    if email_verified is None:
        del claims["email_verified"]
    else:
        claims["email_verified"] = email_verified
    mock_google.return_value = claims

    assert google_login(client).status_code == 401
    with app.app_context():
        assert User.query.count() == 0


@pytest.mark.parametrize("claim", ["sub", "email"])
def test_google_missing_sub_or_email_is_400(app, client, mock_google, user, claim):
    # Without the check a missing sub would match the first account with google_id IS NULL.
    claims = dict(mock_google.return_value)
    del claims[claim]
    mock_google.return_value = claims

    assert google_login(client).status_code == 400
    assert client.get("/api/check-auth").status_code == 401


def test_google_new_account_is_created_and_logged_in(app, client, mock_google):
    response = google_login(client)

    assert response.status_code == 200
    user = find_user(app, "google@example.com")
    assert (user.google_id, user.name, user.password) == ("google-sub-1", "Google User", None)
    assert client.get("/api/check-auth").get_json()["user"]["email"] == "google@example.com"


def test_google_existing_account_logs_in(app, client, mock_google, google_user):
    assert google_login(client).status_code == 200
    assert client.get("/api/check-auth").status_code == 200
    with app.app_context():
        assert User.query.count() == 1


def test_google_email_of_a_password_account_is_refused(app, client, mock_google, user):
    mock_google.return_value = {**mock_google.return_value, "sub": "google-sub-2", "email": "User@Example.com"}

    response = google_login(client)

    assert response.status_code == 400
    assert response.get_json()["message"].startswith("An account with this email already exists")
    assert get_user(app, user.id).google_id is None
    assert client.get("/api/check-auth").status_code == 401


@pytest.fixture(scope="module")
def google_signing():
    """A local RSA key and its self-signed certificate, standing in for Google's signing keys."""
    from datetime import datetime, timedelta, timezone

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from google.auth import crypt

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test signing key")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(1).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return {
        "signer": crypt.RSASigner.from_string(private_pem, key_id="test-kid"),
        "certs": {"test-kid": certificate.public_bytes(serialization.Encoding.PEM).decode()},
    }


@pytest.fixture
def google_certs_endpoint(monkeypatch, google_signing):
    """Answers the certificate download of google-auth; records (url, timeout) of each request."""
    calls = []

    def fake_request(session, method, url, **kwargs):
        calls.append((url, kwargs.get("timeout")))
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(google_signing["certs"]).encode()
        return response

    monkeypatch.setattr(requests.Session, "request", fake_request)
    return calls


def signed_id_token(google_signing, **overrides):
    from google.auth import jwt

    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": TestConfig.GOOGLE_CLIENT_ID,
        "sub": "google-sub-9",
        "email": "real@example.com",
        "email_verified": True,
        "iat": now,
        "exp": now + 600,
        **overrides,
    }
    return jwt.encode(google_signing["signer"], claims).decode()


def test_google_id_token_verification_end_to_end(app, client, google_signing, google_certs_endpoint):
    response = google_login(client, signed_id_token(google_signing))

    assert response.status_code == 200
    assert find_user(app, "real@example.com").google_id == "google-sub-9"
    [(url, timeout)] = google_certs_endpoint
    assert url.startswith("https://www.googleapis.com/")
    assert timeout == 10


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "another-app.apps.googleusercontent.com"},  # a token issued to a different client
        {"iat": int(time.time()) - 7200, "exp": int(time.time()) - 3600},  # expired
        {"iss": "https://evil.example"},
    ],
)
def test_google_id_token_with_wrong_claims_is_401(app, client, google_signing, google_certs_endpoint, overrides):
    response = google_login(client, signed_id_token(google_signing, **overrides))

    assert response.status_code == 401
    with app.app_context():
        assert User.query.count() == 0


def test_google_id_token_signed_by_another_key_is_401(client, google_signing, google_certs_endpoint):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from google.auth import crypt

    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    forged = signed_id_token({"signer": crypt.RSASigner.from_string(other_key, key_id="test-kid")})

    assert google_login(client, forged).status_code == 401


def integrity_error():
    return IntegrityError("INSERT INTO user", {}, Exception("UNIQUE constraint failed"))


def test_google_concurrent_first_sign_in_logs_in_the_account_the_other_request_created(
        app, client, mock_google, monkeypatch):
    real_commit = db.session.commit

    def racing_commit():
        # The other request (a double click) inserts the same Google account first.
        db.session.rollback()
        db.session.add(User(google_id="google-sub-1", email="google@example.com", name="Google User"))
        real_commit()
        raise integrity_error()

    with monkeypatch.context() as patch:
        patch.setattr(db.session, "commit", racing_commit)
        response = google_login(client)

    assert response.status_code == 200
    assert client.get("/api/check-auth").status_code == 200
    with app.app_context():
        assert User.query.count() == 1


def test_google_account_creation_failure_is_500(app, client, mock_google, monkeypatch):
    def failing_commit():
        raise integrity_error()

    with monkeypatch.context() as patch:
        patch.setattr(db.session, "commit", failing_commit)
        response = google_login(client)

    assert response.status_code == 500
    assert response.get_json() == {"message": "An error occurred while creating your account."}
    assert client.get("/api/check-auth").status_code == 401


def test_old_google_path_is_gone(client, mock_google):
    assert client.post("/authorize/google", json={"token": "access-token"}).status_code == 404


# --- forgot / reset password ---------------------------------------------------------------------


def mailed_token(mock_requests):
    text = mock_requests.post.call_args.kwargs["data"]["text"]
    return re.search(r"/reset-password/(\S+)", text).group(1)


def request_reset(client, email):
    return client.post("/api/forgot-password", json={"email": email})


def reset(client, token, password):
    return client.post("/api/reset-password", json={"token": token, "password": password})


def test_forgot_password_answer_is_neutral(client, mock_requests, user, google_user):
    for email in ("nobody@example.com", google_user.email, user.email):
        response = request_reset(client, email)
        assert response.status_code == 200
        assert response.get_json() == {"message": NEUTRAL_MESSAGE}

    # Only the password account is mailed.
    assert mock_requests.post.call_count == 1


def test_forgot_password_mails_the_reset_link(client, mock_requests, user):
    request_reset(client, "USER@example.com")

    call = mock_requests.post.call_args
    assert call.args[0] == f"https://api.mailgun.net/v3/{TestConfig.MAILGUN_DOMAIN}/messages"
    assert call.kwargs["auth"] == ("api", TestConfig.MAILGUN_API_KEY)
    assert call.kwargs["timeout"] > 0
    assert call.kwargs["data"]["to"] == [user.email]
    token = mailed_token(mock_requests)
    assert f"{TestConfig.FRONTEND_URL}/reset-password/{token}" in call.kwargs["data"]["text"]


@pytest.mark.parametrize("failure", ["http_error", "connection_error"])
def test_mailgun_failure_is_logged_and_the_answer_stays_neutral(client, mock_requests, user, caplog, failure):
    if failure == "http_error":
        mock_requests.post.return_value.status_code = 401
        mock_requests.post.return_value.text = "Forbidden: secret detail"
    else:
        mock_requests.post.side_effect = requests.ConnectionError("mailgun unreachable")

    response = request_reset(client, user.email)

    assert response.status_code == 200
    assert response.get_json() == {"message": NEUTRAL_MESSAGE}
    assert f"Password reset email for user {user.id} failed" in caplog.text
    assert user.email not in caplog.text
    assert "secret detail" not in caplog.text


@pytest.mark.parametrize("body", [{}, {"email": "not-an-email"}, {"email": ["a@example.com"]}, None])
def test_forgot_password_rejects_invalid_input(client, mock_requests, body):
    assert client.post("/api/forgot-password", json=body).status_code == 400
    mock_requests.post.assert_not_called()


def test_reset_password_with_a_valid_token(client, mock_requests, user, user_password):
    request_reset(client, user.email)
    token = mailed_token(mock_requests)

    response = reset(client, token, "a-brand-new-password")

    assert response.status_code == 200
    assert response.get_json() == {"message": "Password has been reset successfully!"}
    assert login(client, user.email, "a-brand-new-password").status_code == 200
    assert login(client, user.email, user_password).status_code == 401


def test_reset_works_for_an_account_stored_with_a_mixed_case_email(client, mock_requests, make_user, user_password):
    # Registration used to store the email as typed.
    make_user(email="Legacy.User@Example.com")
    request_reset(client, "legacy.user@example.com")
    token = mailed_token(mock_requests)

    assert reset(client, token, "a-brand-new-password").status_code == 200
    assert login(client, "legacy.user@example.com", "a-brand-new-password").status_code == 200
    assert login(client, "Legacy.User@Example.com", user_password).status_code == 401


def test_reset_token_works_only_once(client, mock_requests, user):
    request_reset(client, user.email)
    token = mailed_token(mock_requests)
    assert reset(client, token, "first-new-password").status_code == 200

    response = reset(client, token, "second-new-password")

    assert response.status_code == 400
    assert response.get_json() == {"message": "Invalid or expired token."}
    assert login(client, user.email, "first-new-password").status_code == 200


def test_tampered_reset_token_is_rejected(client, mock_requests, user):
    request_reset(client, user.email)
    token = mailed_token(mock_requests)
    tampered = token[:-2] + ("AA" if not token.endswith("AA") else "BB")

    assert reset(client, tampered, "a-brand-new-password").status_code == 400


@pytest.mark.parametrize(("age", "status"), [(3599, 200), (3601, 400)])
def test_reset_token_expires_after_an_hour(client, mock_requests, user, age, status):
    with freeze_time("2026-01-01 12:00:00") as frozen:
        request_reset(client, user.email)
        token = mailed_token(mock_requests)
        frozen.tick(age)

        assert reset(client, token, "a-brand-new-password").status_code == status


def test_reset_rejects_a_bad_password_and_keeps_the_token_usable(client, mock_requests, user):
    request_reset(client, user.email)
    token = mailed_token(mock_requests)

    for password in ("12345", "x" * 129, None, 123456):
        assert reset(client, token, password).status_code == 400

    assert reset(client, token, "a-brand-new-password").status_code == 200


def test_reset_for_a_deleted_user_is_404(app, client, mock_requests, user):
    request_reset(client, user.email)
    token = mailed_token(mock_requests)
    with app.app_context():
        db.session.delete(db.session.get(User, user.id))
        db.session.commit()

    response = reset(client, token, "a-brand-new-password")

    assert response.status_code == 404
    assert response.get_json() == {"message": "User not found."}


def test_reset_rejects_tokens_of_the_old_format_and_bodies_without_a_token(app, client, user):
    from itsdangerous import URLSafeTimedSerializer

    # Old tokens signed only the email.
    old_token = URLSafeTimedSerializer(TestConfig.SECRET_KEY).dumps(user.email, salt="password-reset-salt")
    assert reset(client, old_token, "a-brand-new-password").status_code == 400
    for body in ({}, {"password": "a-brand-new-password"}, {"token": 5, "password": "x" * 8}, None):
        assert client.post("/api/reset-password", json=body).status_code == 400


def test_token_in_the_url_path_is_no_longer_accepted(client, mock_requests, user):
    request_reset(client, user.email)
    token = mailed_token(mock_requests)

    response = client.post(f"/api/reset-password/{token}", json={"password": "a-brand-new-password"})

    assert response.status_code == 404
