import secrets

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


def _new_session_token():
    return secrets.token_urlsafe(32)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    # Complimentary data access; set only by `flask make-admin <email>`.
    superuser = db.Column(db.Boolean, default=False)
    google_id = db.Column(db.String(255), unique=True, nullable=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    name = db.Column(db.String(255))
    password = db.Column(db.String(255), nullable=True)

    # New fields for managing subscriptions
    stripe_customer_id = db.Column(db.String(255), unique=True, nullable=True)
    subscription_status = db.Column(db.String(255), nullable=True)  # 'active', 'canceled', etc.

    # Part of the login cookie; rotating it invalidates every existing session and remember cookie.
    session_token = db.Column(db.String(64), nullable=False, default=_new_session_token)

    def set_password(self, password):
        self.password = generate_password_hash(password, method="pbkdf2:sha256")

    def check_password(self, password):
        """Return False for accounts without a password (Google sign-in only)."""
        if not self.password:
            return False
        return check_password_hash(self.password, password)

    def get_id(self):
        return f"{self.id}:{self.session_token}"

    def rotate_session_token(self):
        self.session_token = _new_session_token()


class StripeEvent(db.Model):
    """A processed Stripe webhook event; its id makes redeliveries a no-op."""

    id = db.Column(db.String(255), primary_key=True)
    created = db.Column(db.Integer)
    type = db.Column(db.String(255))
