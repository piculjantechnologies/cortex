import functools
import hashlib
import hmac
import re

import google.auth.exceptions
import google.auth.transport.requests
import requests
import stripe
from flask import Blueprint, current_app, jsonify, request, session
from flask_limiter.util import get_remote_address
from flask_login import current_user, login_required, login_user, logout_user
from google.oauth2 import id_token
from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy.exc import IntegrityError

from ..extensions import db, limiter, login_manager
from ..models import User

bp = Blueprint("auth", __name__)

MAX_EMAIL_LENGTH = 254
MIN_PASSWORD_LENGTH = 6
MAX_PASSWORD_LENGTH = 128
EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

RESET_SALT = "password-reset-salt"
RESET_TOKEN_MAX_AGE = 3600  # seconds
FORGOT_PASSWORD_MESSAGE = "If an account exists for that email, a reset link has been sent."

OUTBOUND_TIMEOUT = 10  # seconds, for Mailgun and Google's certificate endpoint

# Subscriptions in these states are already over; deleting an account cancels every other one.
TERMINAL_SUBSCRIPTION_STATUSES = ("canceled", "incomplete_expired")


@login_manager.user_loader
def load_user(user_id):
    """Load the user from the 'id:session_token' login id; a rotated token ends every session."""
    raw_id, _, token = user_id.partition(":")
    if not token:
        return None
    try:
        user = db.session.get(User, int(raw_id))
    except ValueError:
        return None
    if user is None or not hmac.compare_digest(user.session_token, token):
        return None
    return user


def _json_body():
    """The request's JSON object, or {} for a missing, malformed or non-object body."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _normalize_email(value):
    return value.strip().lower() if isinstance(value, str) else ""


def _is_valid_email(email):
    return len(email) <= MAX_EMAIL_LENGTH and EMAIL_PATTERN.fullmatch(email) is not None


def _password_error(password):
    """The message for an unacceptable new password, or None."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters long."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Password must be at most {MAX_PASSWORD_LENGTH} characters long."
    return None


def _find_user_by_email(email):
    # Case-insensitive, so accounts stored before emails were lowercased are still found, also
    # from the stored mixed-case address that a reset token carries.
    return User.query.filter(db.func.lower(User.email) == email.lower()).first()


def _log_in(user):
    session.permanent = True  # the session cookie lives as long as the remember cookie
    login_user(user, remember=True)


def _email_rate_limit_key():
    return "email:" + _normalize_email(_json_body().get("email"))


@bp.route("/api/authorize/google", methods=["POST"])
@limiter.limit("10/minute")
def google_auth():
    credential = _json_body().get("credential")
    if not isinstance(credential, str) or not credential:
        return jsonify({"message": "Credential missing"}), 400

    # Checks the signature against Google's certificates, the audience (our client id) and expiry.
    transport = functools.partial(google.auth.transport.requests.Request(), timeout=OUTBOUND_TIMEOUT)
    try:
        claims = id_token.verify_oauth2_token(credential, transport, current_app.config["GOOGLE_CLIENT_ID"])
    except google.auth.exceptions.TransportError:
        current_app.logger.warning("Could not fetch Google's signing certificates", exc_info=True)
        return jsonify({"message": "Google sign-in is unavailable, please try again later."}), 502
    except (ValueError, google.auth.exceptions.GoogleAuthError) as e:
        current_app.logger.info("Rejected Google ID token: %s", e)
        return jsonify({"message": "Failed to authenticate"}), 401

    google_id = claims.get("sub")
    email = _normalize_email(claims.get("email"))
    if not google_id or not email:
        return jsonify({"message": "Failed to authenticate"}), 400
    if claims.get("email_verified") is not True:
        return jsonify({"message": "Failed to authenticate"}), 401

    user = User.query.filter_by(google_id=google_id).first()
    if user is None:
        if _find_user_by_email(email):
            return jsonify({"message": "An account with this email already exists. "
                                       "Please log in using your email and password."}), 400
        user = User(google_id=google_id, email=email, name=claims.get("name"))
        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError:
            # A concurrent request created the account first.
            db.session.rollback()
            user = User.query.filter_by(google_id=google_id).first()
            if user is None:
                return jsonify({"message": "An error occurred while creating your account."}), 500

    _log_in(user)
    return jsonify({"message": "Login successful"}), 200


@bp.route("/api/logout", methods=["POST"])
@login_required
def logout():
    # A new session token invalidates this user's session and remember cookies everywhere.
    current_user.rotate_session_token()
    db.session.commit()
    logout_user()
    return jsonify({"message": "Logged out successfully"}), 200


@bp.route("/api/user", methods=["GET"])
@login_required
def get_user():
    user_info = {"email": current_user.email}
    return jsonify(user_info), 200


@bp.route("/api/check-auth", methods=["GET"])
def check_auth():
    if current_user.is_authenticated:
        user_info = {
            "name": current_user.name,
            "email": current_user.email,
        }
        return jsonify({"authenticated": True, "user": user_info}), 200
    else:
        return jsonify({"authenticated": False}), 401


# Email/Password login route
@bp.route("/api/login", methods=["POST"])
@limiter.limit("10/minute")
def login():
    data = _json_body()
    email = _normalize_email(data.get("email"))
    password = data.get("password")

    if not email or not isinstance(password, str) or not password:
        return jsonify({"message": "Email and password are required"}), 400

    user = _find_user_by_email(email)
    # check_password is False for Google-only accounts, which have no password.
    if not user or not user.check_password(password):
        return jsonify({"message": "Invalid credentials"}), 401

    _log_in(user)
    return jsonify({"message": "Login successful"}), 200


# Email/Password registration route
@bp.route("/api/register", methods=["POST"])
@limiter.limit("10/minute")
def register():
    data = _json_body()
    email = _normalize_email(data.get("email"))
    password = data.get("password")

    if not email or not isinstance(password, str) or not password:
        return jsonify({"message": "All fields are required"}), 400
    if not _is_valid_email(email):
        return jsonify({"message": "Invalid email address."}), 400
    error = _password_error(password)
    if error:
        return jsonify({"message": error}), 400

    if _find_user_by_email(email):
        return jsonify({"message": "User already exists"}), 400

    new_user = User(email=email)
    new_user.set_password(password)
    db.session.add(new_user)
    try:
        db.session.commit()
    except IntegrityError:
        # A concurrent registration with the same email won the race.
        db.session.rollback()
        return jsonify({"message": "User already exists"}), 409

    return jsonify({"message": "User registered successfully"}), 201


def _serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"])


def _password_fingerprint(user):
    # Signed into the reset token, so the token stops working once the password changes.
    return hashlib.sha256((user.password or "").encode()).hexdigest()[:16]


# Function to send email via Mailgun API
def send_mailgun_email(to_email, subject, text):
    """Send an email using the Mailgun API."""
    domain = current_app.config["MAILGUN_DOMAIN"]
    return requests.post(
        f"https://api.mailgun.net/v3/{domain}/messages",
        auth=("api", current_app.config["MAILGUN_API_KEY"]),
        data={
            "from": f"Cortex <mailgun@{domain}>",
            "to": [to_email],
            "subject": subject,
            "text": text,
        },
        timeout=OUTBOUND_TIMEOUT,
    )


# Function to send password reset email
def send_reset_email(user, reset_token):
    """Mail the reset link; failures are logged without the address or Mailgun's reply."""
    reset_url = f"{current_app.config['FRONTEND_URL']}/reset-password/{reset_token}"

    subject = "Password Reset Request"
    body = f"Click the link to reset your password: {reset_url}\n" \
           "If you did not request this, please ignore this email."

    try:
        response = send_mailgun_email(user.email, subject, body)
    except requests.RequestException as e:
        current_app.logger.warning("Password reset email for user %s failed: %s", user.id, type(e).__name__)
        return False
    if response.status_code != 200:
        current_app.logger.warning(
            "Password reset email for user %s failed: Mailgun answered HTTP %s", user.id, response.status_code
        )
        return False
    current_app.logger.info("Password reset email sent for user %s", user.id)
    return True


# Route to generate and send password reset link
@bp.route("/api/forgot-password", methods=["POST"])
@limiter.limit("3/hour", key_func=_email_rate_limit_key)
@limiter.limit("3/hour", key_func=get_remote_address)
def forgot_password():
    email = _normalize_email(_json_body().get("email"))
    if not _is_valid_email(email):
        return jsonify({"message": "A valid email address is required."}), 400

    # Same answer whether or not the account exists; only password accounts get a link. The mail
    # is sent inline, so the response takes longer for a password account; that reveals no more
    # than /api/register's "User already exists" does.
    user = _find_user_by_email(email)
    if user is not None and user.password:
        token = _serializer().dumps({"email": user.email, "pw": _password_fingerprint(user)}, salt=RESET_SALT)
        send_reset_email(user, token)

    return jsonify({"message": FORGOT_PASSWORD_MESSAGE}), 200


def _invalid_token():
    return jsonify({"message": "Invalid or expired token."}), 400


# Route to handle resetting the password; the token comes in the body, never in the URL.
@bp.route("/api/reset-password", methods=["POST"])
def reset_password():
    data = _json_body()
    token = data.get("token")
    if not isinstance(token, str) or not token:
        return _invalid_token()
    try:
        payload = _serializer().loads(token, salt=RESET_SALT, max_age=RESET_TOKEN_MAX_AGE)
    except BadData:
        return _invalid_token()
    if not isinstance(payload, dict) or not all(isinstance(payload.get(key), str) for key in ("email", "pw")):
        return _invalid_token()

    user = _find_user_by_email(payload["email"])
    if not user:
        return jsonify({"message": "User not found."}), 404
    if not hmac.compare_digest(payload["pw"], _password_fingerprint(user)):
        return _invalid_token()  # already used: the password changed after the token was issued

    new_password = data.get("password")
    error = _password_error(new_password)
    if error:
        return jsonify({"message": error}), 400

    user.set_password(new_password)
    user.rotate_session_token()  # ends every existing session and remember cookie
    db.session.commit()

    return jsonify({"message": "Password has been reset successfully!"}), 200


@bp.route("/api/delete-account", methods=["DELETE"])
@login_required
def delete_account():
    user = current_user._get_current_object()
    if user.stripe_customer_id and not _cancel_billing(user):
        return jsonify({"error": "Could not cancel the subscription; the account was not deleted."}), 502

    db.session.delete(user)
    db.session.commit()
    logout_user()
    return jsonify({"message": "Account deleted successfully"}), 200


def _cancel_billing(user):
    """Cancel the user's open subscriptions and delete the Stripe customer; False if Stripe fails."""
    api_key = current_app.config["STRIPE_API_KEY"]
    customer_id = user.stripe_customer_id
    try:
        subscriptions = stripe.Subscription.list(customer=customer_id, status="all", api_key=api_key)
        for subscription in subscriptions.auto_paging_iter():
            if subscription.status not in TERMINAL_SUBSCRIPTION_STATUSES:
                _cancel_subscription(subscription.id, api_key)
        stripe.Customer.delete(customer_id, api_key=api_key)
    except stripe.InvalidRequestError as e:
        # Only the customer calls (list, delete) get here with resource_missing.
        if e.code == "resource_missing":  # the customer was already deleted in Stripe
            return True
        current_app.logger.exception("Stripe cleanup failed for user %s", user.id)
        return False
    except stripe.StripeError:
        current_app.logger.exception("Stripe cleanup failed for user %s", user.id)
        return False
    return True


def _cancel_subscription(subscription_id, api_key):
    try:
        stripe.Subscription.cancel(subscription_id, api_key=api_key)
    except stripe.InvalidRequestError as e:
        # Removed between the list and this call: nothing to cancel, go on with the others.
        if e.code != "resource_missing":
            raise
