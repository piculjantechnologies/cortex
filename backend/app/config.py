"""Application settings.

``ProdConfig.from_env()`` reads the environment when ``create_app()`` runs (not
at import) and refuses to start when a required variable is missing.
``TestConfig`` is self-contained and needs no environment at all.
"""

import os
import warnings
from datetime import timedelta
from urllib.parse import urlsplit

from sqlalchemy.engine import URL


class ConfigError(RuntimeError):
    """A required setting is missing or invalid."""


def _int_env(name, default):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"Environment variable {name} must be an integer, got {raw!r}") from None


class BaseConfig:
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # A login lasts 30 days: the session cookie and the remember cookie expire together.
    PERMANENT_SESSION_LIFETIME = timedelta(days=30)
    REMEMBER_COOKIE_DURATION = PERMANENT_SESSION_LIFETIME
    SESSION_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True

    MONGO_DB = "cortex"
    MONGO_COLLECTION = "collection"

    RATELIMIT_ENABLED = True
    RATELIMIT_STORAGE_URI = "memory://"

    # Upper bound on the rows of one CSV export.
    EXPORT_MAX_ROWS = 100_000
    # MongoDB time limits (maxTimeMS) of one search page (and its count) and of one CSV export.
    QUERY_MAX_TIME_MS = 10_000
    EXPORT_MAX_TIME_MS = 300_000

    # Number of reverse proxies in front of the app whose X-Forwarded-For entry is trusted.
    TRUSTED_PROXY_COUNT = 0


class ProdConfig(BaseConfig):
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 28800,  # Match MySQL's wait_timeout
        "pool_size": 15,
        "max_overflow": 25,
        "pool_timeout": 20,  # Timeout for acquiring connections from the pool
    }

    @classmethod
    def from_env(cls):
        """Build the configuration from os.environ, failing fast on missing variables."""
        missing = []

        def require(name):
            value = os.environ.get(name, "")
            if not value.strip():
                missing.append(name)
            return value

        config = cls()
        config.SECRET_KEY = require("SECRET_KEY")
        # Normalised before the check, so "/" counts as missing instead of becoming "".
        config.FRONTEND_URL = os.environ.get("FRONTEND_URL", "").strip().rstrip("/")
        if not config.FRONTEND_URL:
            missing.append("FRONTEND_URL")
        db_user = require("DB_USER")
        db_password = require("DB_PASSWORD")
        db_host = require("DB_HOST")
        db_name = require("DB_NAME")
        config.STRIPE_API_KEY = require("STRIPE_API_KEY")
        config.STRIPE_PRICE_ID = require("STRIPE_PRICE_ID")
        config.STRIPE_WEBHOOK_SECRET = require("STRIPE_WEBHOOK_SECRET")
        config.STRIPE_SUCCESS_URL = require("STRIPE_SUCCESS_URL")
        config.STRIPE_CANCEL_URL = require("STRIPE_CANCEL_URL")
        config.MAILGUN_API_KEY = require("MAILGUN_API_KEY")
        config.MAILGUN_DOMAIN = require("MAILGUN_DOMAIN")
        config.GOOGLE_CLIENT_ID = require("GOOGLE_CLIENT_ID")

        mongo_uri = os.environ.get("MONGO_URI", "").strip()
        if not mongo_uri:
            mongo_uri = os.environ.get("mongo_db_uri", "").strip()
            if mongo_uri:
                warnings.warn(
                    "The mongo_db_uri environment variable is deprecated and will be removed "
                    "in the next release; set MONGO_URI instead.",
                    FutureWarning,
                    stacklevel=2,
                )
            else:
                missing.append("MONGO_URI")
        config.MONGO_URI = mongo_uri

        if missing:
            raise ConfigError("Missing required environment variables: " + ", ".join(missing))

        # The CORS origin and the base of the reset links: it must be an absolute http(s) URL.
        frontend = urlsplit(config.FRONTEND_URL)
        if frontend.scheme not in ("http", "https") or not frontend.netloc:
            raise ConfigError(f"FRONTEND_URL must be an absolute http(s) URL, got {config.FRONTEND_URL!r}")

        config.SQLALCHEMY_DATABASE_URI = URL.create(
            "mysql+pymysql",
            username=db_user,
            password=db_password,
            host=db_host,
            port=_int_env("DB_PORT", 3306),
            database=db_name,
        )
        ssl_ca = os.environ.get("DB_SSL_CA", "").strip()
        if ssl_ca:
            config.SQLALCHEMY_ENGINE_OPTIONS = {
                **cls.SQLALCHEMY_ENGINE_OPTIONS,
                "connect_args": {"ssl": {"ca": ssl_ca}},
            }

        config.MONGO_DB = os.environ.get("MONGO_DB", "").strip() or cls.MONGO_DB
        config.MONGO_COLLECTION = os.environ.get("MONGO_COLLECTION", "").strip() or cls.MONGO_COLLECTION

        # Secure cookies need HTTPS; COOKIE_SECURE=0 is only for local development over http.
        secure = os.environ.get("COOKIE_SECURE", "1").strip() != "0"
        config.SESSION_COOKIE_SECURE = secure
        config.REMEMBER_COOKIE_SECURE = secure

        config.RATELIMIT_STORAGE_URI = (
            os.environ.get("RATELIMIT_STORAGE_URI", "").strip() or cls.RATELIMIT_STORAGE_URI
        )

        config.EXPORT_MAX_ROWS = _int_env("EXPORT_MAX_ROWS", cls.EXPORT_MAX_ROWS)
        if config.EXPORT_MAX_ROWS < 1:
            raise ConfigError("EXPORT_MAX_ROWS must be at least 1")
        for name in ("QUERY_MAX_TIME_MS", "EXPORT_MAX_TIME_MS"):
            setattr(config, name, _int_env(name, getattr(cls, name)))
            if getattr(config, name) < 1:
                raise ConfigError(f"{name} must be at least 1")

        # The documented deployment runs gunicorn behind one reverse proxy.
        config.TRUSTED_PROXY_COUNT = _int_env("TRUSTED_PROXY_COUNT", 1)
        if config.TRUSTED_PROXY_COUNT < 0:
            raise ConfigError("TRUSTED_PROXY_COUNT must not be negative")

        return config


class TestConfig(BaseConfig):
    __test__ = False  # keeps pytest from collecting this class

    TESTING = True
    SECRET_KEY = "test"
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    SQLALCHEMY_ENGINE_OPTIONS = {}
    RATELIMIT_ENABLED = False

    FRONTEND_URL = "http://localhost:3000"
    GOOGLE_CLIENT_ID = "test-client-id.apps.googleusercontent.com"
    STRIPE_API_KEY = "stripe-test-api-key"
    STRIPE_PRICE_ID = "price_test"
    STRIPE_WEBHOOK_SECRET = "stripe-test-webhook-secret"
    STRIPE_SUCCESS_URL = "http://localhost:3000/dashboard"
    STRIPE_CANCEL_URL = "http://localhost:3000/dashboard"
    MAILGUN_API_KEY = "mailgun-test-api-key"
    MAILGUN_DOMAIN = "mg.example.test"
