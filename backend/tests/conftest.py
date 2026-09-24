"""Shared fixtures for the web app tests.

Everything web-specific (flask, the app package, mongomock, stripe) is imported
inside the fixtures, so this file also loads where only the pipeline
requirements are installed (``pytest -m pipeline``). In such an environment the
web test modules in this directory are not collected at all.
"""

import copy
import hashlib
import hmac
import importlib.util
import itertools
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

if importlib.util.find_spec("flask") is None:
    collect_ignore_glob = ["test_*.py"]

USER_PASSWORD = "correct-horse-battery"


@pytest.fixture
def app():
    """A fresh app per test, built from TestConfig on an in-memory SQLite database."""
    from flask_login import FlaskLoginClient

    from app import create_app
    from app.config import TestConfig
    from app.extensions import db

    app = create_app(TestConfig)
    app.test_client_class = FlaskLoginClient
    with app.app_context():
        db.create_all()

    # No app context stays pushed during the test: each request gets its own, like in production.
    yield app

    with app.app_context():
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


@pytest.fixture
def client(app):
    """An anonymous test client."""
    return app.test_client()


@pytest.fixture
def user_password():
    """The plain-text password of every password account made by make_user."""
    return USER_PASSWORD


@pytest.fixture
def make_user(app):
    """Factory: make_user(email=..., password=..., **columns) -> a committed, detached User.

    Pass password=None for a Google-only account. The returned object has every
    column loaded, so it can be read outside an app context.
    """
    from app.extensions import db
    from app.models import User

    counter = itertools.count(1)

    def _make_user(email=None, password=USER_PASSWORD, **columns):
        with app.app_context():
            user = User(email=email or f"user{next(counter)}@example.com", **columns)
            if password is not None:
                user.set_password(password)
            db.session.add(user)
            db.session.commit()
            db.session.refresh(user)
            db.session.expunge(user)
        return user

    return _make_user


@pytest.fixture
def user(make_user):
    """A password account without a subscription."""
    return make_user(email="user@example.com", name="Test User")


@pytest.fixture
def google_user(make_user):
    """A Google-only account (no password)."""
    return make_user(email="google@example.com", password=None, google_id="google-sub-1", name="Google User")


@pytest.fixture
def subscriber(make_user):
    """A password account with an active subscription."""
    return make_user(
        email="subscriber@example.com",
        name="Subscriber",
        stripe_customer_id="cus_test_subscriber",
        subscription_status="active",
    )


@pytest.fixture
def superuser(make_user):
    """A password account with complimentary access (superuser flag, no subscription)."""
    return make_user(email="admin@example.com", name="Admin", superuser=True)


@pytest.fixture
def login_as(app):
    """Factory: login_as(user) -> a test client whose session is logged in as that user."""

    def _login_as(user):
        return app.test_client(user=user)

    return _login_as


@pytest.fixture
def logged_in_client(login_as, user):
    """A test client logged in as the `user` fixture."""
    return login_as(user)


class RecordingCollection:
    """A mongomock collection that also records every aggregate pipeline it runs."""

    def __init__(self, collection):
        self.collection = collection
        self.pipelines = []
        self.options = []

    def aggregate(self, pipeline, *args, **kwargs):
        self.pipelines.append(copy.deepcopy(pipeline))
        self.options.append(kwargs)
        return self.collection.aggregate(pipeline, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.collection, name)


@pytest.fixture
def fake_mongo(monkeypatch):
    """Replace the routes' Mongo collection with a seeded, recording mongomock collection.

    Seeded with 30 documents that match the default query (width 640, height 480,
    score >= 0.5) and 2 'error: broken image' documents.
    """
    import mongomock

    collection = mongomock.MongoClient().cortex.collection
    classes = ["person", "dog", "car"]
    docs = [
        {
            "url": f"https://images.example.com/{i}.jpg",
            "width": 640,
            "height": 480,
            "object_detection": {classes[i % len(classes)]: [[0.1, 0.1, 0.5, 0.5]]},
            "object_counts": {classes[i % len(classes)]: 1},
            "object_max_area": {classes[i % len(classes)]: 0.16},
            "object_total": 1,
            "label_quality_score": 0.5 + i / 100,
        }
        for i in range(30)
    ]
    docs += [
        {"url": f"https://images.example.com/broken{i}.jpg", "object_detection": "error: broken image"}
        for i in range(2)
    ]
    collection.insert_many(docs)

    recording = RecordingCollection(collection)
    monkeypatch.setattr("app.routes.cortex.get_mongo_collection", lambda: recording)
    return recording


@pytest.fixture
def mock_stripe(monkeypatch):
    """MagicMocks for every Stripe call the routes make; no request leaves the process.

    Default return values are real Stripe objects, so both attribute and item access work.
    Build more with mock_stripe.obj(cls, **fields) and mock_stripe.list_of(*objects).
    """
    import stripe

    def obj(cls, **fields):
        return cls.construct_from(fields, "stripe-test-api-key")

    def list_of(*objects):
        return stripe.ListObject.construct_from(
            {"object": "list", "data": list(objects), "has_more": False, "url": "/v1/list"},
            "stripe-test-api-key",
        )

    subscription = obj(stripe.Subscription, id="sub_test", object="subscription", status="active",
                       customer="cus_test_subscriber")
    mocks = SimpleNamespace(
        obj=obj,
        list_of=list_of,
        customer_create=MagicMock(return_value=obj(stripe.Customer, id="cus_test_new", object="customer")),
        customer_delete=MagicMock(return_value=obj(stripe.Customer, id="cus_test_new", object="customer",
                                                   deleted=True)),
        checkout_session_create=MagicMock(return_value=obj(
            stripe.checkout.Session, id="cs_test", object="checkout.session",
            url="https://checkout.stripe.com/c/pay/cs_test",
        )),
        portal_session_create=MagicMock(return_value=obj(
            stripe.billing_portal.Session, id="bps_test", object="billing_portal.session",
            url="https://billing.stripe.com/p/session/bps_test",
        )),
        subscription_list=MagicMock(return_value=list_of()),
        subscription_retrieve=MagicMock(return_value=subscription),
        subscription_cancel=MagicMock(return_value=subscription),
    )
    monkeypatch.setattr(stripe.Customer, "create", mocks.customer_create)
    monkeypatch.setattr(stripe.Customer, "delete", mocks.customer_delete)
    monkeypatch.setattr(stripe.checkout.Session, "create", mocks.checkout_session_create)
    monkeypatch.setattr(stripe.billing_portal.Session, "create", mocks.portal_session_create)
    monkeypatch.setattr(stripe.Subscription, "list", mocks.subscription_list)
    monkeypatch.setattr(stripe.Subscription, "retrieve", mocks.subscription_retrieve)
    monkeypatch.setattr(stripe.Subscription, "cancel", mocks.subscription_cancel)
    return mocks


@pytest.fixture
def mock_google(monkeypatch):
    """Replace google.oauth2.id_token.verify_oauth2_token; by default it accepts the token.

    Set .return_value to other claims, or .side_effect = ValueError(...) for a rejected token.
    """
    from google.oauth2 import id_token

    from app.config import TestConfig

    verify = MagicMock(return_value={
        "iss": "https://accounts.google.com",
        "aud": TestConfig.GOOGLE_CLIENT_ID,
        "sub": "google-sub-1",
        "email": "google@example.com",
        "email_verified": True,
        "name": "Google User",
    })
    monkeypatch.setattr(id_token, "verify_oauth2_token", verify)
    return verify


@pytest.fixture
def mock_requests(monkeypatch):
    """Replace requests.post (the Mailgun call); by default Mailgun answers 200.

    Inspect mock_requests.post.call_args; set .post.return_value.status_code or
    .post.side_effect to simulate failures.
    """
    import requests

    response = MagicMock(status_code=200, ok=True, text='{"message": "Queued. Thank you."}')
    response.json.return_value = {"id": "<test@mg.example.test>", "message": "Queued. Thank you."}
    post = MagicMock(return_value=response)
    monkeypatch.setattr(requests, "post", post)
    return SimpleNamespace(post=post)


def _stripe_signature(payload, secret, timestamp=None):
    """The Stripe-Signature header value Stripe would send for this payload."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    timestamp = int(time.time()) if timestamp is None else int(timestamp)
    digest = hmac.new(secret.encode("utf-8"), f"{timestamp}.{payload}".encode("utf-8"), hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


@pytest.fixture
def stripe_sig():
    """stripe_sig(payload, secret, timestamp=None) -> a valid Stripe-Signature header value."""
    return _stripe_signature
