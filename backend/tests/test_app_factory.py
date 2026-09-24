import runpy
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from flask import Flask, request
from sqlalchemy.engine import make_url

from app import create_app, get_mongo_collection
from app.config import ConfigError, ProdConfig, TestConfig
from app.extensions import db, limiter, login_manager
from app.models import StripeEvent, User

BACKEND_DIR = Path(__file__).resolve().parents[1]

PROD_ENV = {
    "SECRET_KEY": "prod-secret-key",
    "FRONTEND_URL": "https://cortex.example.com",
    "DB_USER": "cortex",
    "DB_PASSWORD": "p@ss:w/rd%40",
    "DB_HOST": "db.example.com",
    "DB_NAME": "cortex",
    "STRIPE_API_KEY": "stripe-api-key",
    "STRIPE_PRICE_ID": "price_123",
    "STRIPE_WEBHOOK_SECRET": "stripe-webhook-secret",
    "STRIPE_SUCCESS_URL": "https://cortex.example.com/dashboard",
    "STRIPE_CANCEL_URL": "https://cortex.example.com/dashboard",
    "MAILGUN_API_KEY": "mailgun-api-key",
    "MAILGUN_DOMAIN": "mg.example.com",
    "GOOGLE_CLIENT_ID": "client-id.apps.googleusercontent.com",
    "MONGO_URI": "mongodb://mongo.example.com:27017",
}
OPTIONAL_ENV = [
    "DB_PORT",
    "DB_SSL_CA",
    "MONGO_DB",
    "MONGO_COLLECTION",
    "mongo_db_uri",
    "COOKIE_SECURE",
    "RATELIMIT_STORAGE_URI",
    "EXPORT_MAX_ROWS",
    "QUERY_MAX_TIME_MS",
    "EXPORT_MAX_TIME_MS",
    "TRUSTED_PROXY_COUNT",
    "FLASK_DEBUG",
]


@pytest.fixture
def prod_env(monkeypatch):
    """A complete production environment; optional variables from the developer's shell are cleared."""
    for name in OPTIONAL_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in PROD_ENV.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


@pytest.fixture
def build_app():
    """build_app(config=None) -> create_app(config); engines are disposed after the test."""
    apps = []

    def _build(config=None):
        app = create_app(config)
        apps.append(app)
        return app

    yield _build
    for app in apps:
        with app.app_context():
            db.engine.dispose()


# --- configuration -----------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(PROD_ENV))
def test_missing_required_variable_is_named(prod_env, name):
    prod_env.delenv(name)
    with pytest.raises(ConfigError, match=name):
        create_app()


@pytest.mark.parametrize("value", ["", "   ", "/", " // "])
def test_empty_frontend_url_is_rejected(prod_env, value):
    prod_env.setenv("FRONTEND_URL", value)
    with pytest.raises(ConfigError, match="Missing required environment variables: FRONTEND_URL"):
        create_app()


@pytest.mark.parametrize("value", ["cortex.example.com", "/dashboard", "ftp://cortex.example.com", "https://"])
def test_frontend_url_must_be_an_absolute_http_url(prod_env, value):
    prod_env.setenv("FRONTEND_URL", value)
    with pytest.raises(ConfigError, match="FRONTEND_URL must be an absolute http"):
        create_app()


def test_frontend_url_loses_its_trailing_slash(prod_env, build_app):
    prod_env.setenv("FRONTEND_URL", " http://localhost:3000/ ")
    assert build_app().config["FRONTEND_URL"] == "http://localhost:3000"


def test_all_missing_variables_are_listed_at_once(prod_env):
    for name in ("SECRET_KEY", "DB_HOST", "MAILGUN_DOMAIN"):
        prod_env.delenv(name)
    with pytest.raises(ConfigError, match="SECRET_KEY, DB_HOST, MAILGUN_DOMAIN"):
        create_app()


def test_prod_config_comes_from_the_environment(prod_env, build_app):
    app = build_app()
    config = app.config

    assert config["SECRET_KEY"] == "prod-secret-key"
    assert config["FRONTEND_URL"] == "https://cortex.example.com"
    assert config["GOOGLE_CLIENT_ID"] == PROD_ENV["GOOGLE_CLIENT_ID"]
    assert config["STRIPE_WEBHOOK_SECRET"] == "stripe-webhook-secret"
    assert config["MAILGUN_DOMAIN"] == "mg.example.com"

    uri = config["SQLALCHEMY_DATABASE_URI"]
    assert (uri.drivername, uri.username, uri.host, uri.port, uri.database) == (
        "mysql+pymysql", "cortex", "db.example.com", 3306, "cortex"
    )
    # '@', ':', '/' and '%' in the password survive a round trip through the URL string.
    assert make_url(uri.render_as_string(hide_password=False)).password == "p@ss:w/rd%40"
    assert config["SQLALCHEMY_ENGINE_OPTIONS"] == ProdConfig.SQLALCHEMY_ENGINE_OPTIONS
    assert "connect_args" not in config["SQLALCHEMY_ENGINE_OPTIONS"]
    with app.app_context():
        assert db.engine.url.drivername == "mysql+pymysql"
        assert db.engine.pool.size() == 15

    assert config["MONGO_URI"] == "mongodb://mongo.example.com:27017"
    assert (config["MONGO_DB"], config["MONGO_COLLECTION"]) == ("cortex", "collection")
    assert config["RATELIMIT_ENABLED"] is True
    assert config["RATELIMIT_STORAGE_URI"] == "memory://"
    assert config["EXPORT_MAX_ROWS"] == 100_000
    assert (config["QUERY_MAX_TIME_MS"], config["EXPORT_MAX_TIME_MS"]) == (10_000, 300_000)
    assert config["TRUSTED_PROXY_COUNT"] == 1


def test_cookie_and_login_lifetime_settings(prod_env, build_app):
    config = build_app().config
    assert config["PERMANENT_SESSION_LIFETIME"] == timedelta(days=30)
    assert config["REMEMBER_COOKIE_DURATION"] == timedelta(days=30)
    for prefix in ("SESSION", "REMEMBER"):
        assert config[f"{prefix}_COOKIE_SAMESITE"] == "Lax"
        assert config[f"{prefix}_COOKIE_HTTPONLY"] is True
        assert config[f"{prefix}_COOKIE_SECURE"] is True
    assert login_manager.session_protection == "basic"


def test_cookie_secure_can_be_disabled_for_local_http(prod_env, build_app):
    prod_env.setenv("COOKIE_SECURE", "0")
    config = build_app().config
    assert config["SESSION_COOKIE_SECURE"] is False
    assert config["REMEMBER_COOKIE_SECURE"] is False


def test_optional_variables_override_defaults(prod_env):
    prod_env.setenv("FRONTEND_URL", "https://cortex.example.com/")
    prod_env.setenv("DB_PORT", "3307")
    prod_env.setenv("DB_SSL_CA", "certs/ca.pem")
    prod_env.setenv("MONGO_DB", "cortex_staging")
    prod_env.setenv("MONGO_COLLECTION", "images")
    prod_env.setenv("RATELIMIT_STORAGE_URI", "memory://staging")
    prod_env.setenv("EXPORT_MAX_ROWS", "500")
    prod_env.setenv("QUERY_MAX_TIME_MS", "2500")
    prod_env.setenv("EXPORT_MAX_TIME_MS", "60000")
    prod_env.setenv("TRUSTED_PROXY_COUNT", "2")

    config = ProdConfig.from_env()

    assert config.FRONTEND_URL == "https://cortex.example.com"
    assert config.SQLALCHEMY_DATABASE_URI.port == 3307
    assert config.SQLALCHEMY_ENGINE_OPTIONS == {
        **ProdConfig.SQLALCHEMY_ENGINE_OPTIONS,
        "connect_args": {"ssl": {"ca": "certs/ca.pem"}},
    }
    assert "connect_args" not in ProdConfig.SQLALCHEMY_ENGINE_OPTIONS
    assert (config.MONGO_DB, config.MONGO_COLLECTION) == ("cortex_staging", "images")
    assert config.RATELIMIT_STORAGE_URI == "memory://staging"
    assert config.EXPORT_MAX_ROWS == 500
    assert (config.QUERY_MAX_TIME_MS, config.EXPORT_MAX_TIME_MS) == (2500, 60000)
    assert config.TRUSTED_PROXY_COUNT == 2


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DB_PORT", "mysql"),
        ("EXPORT_MAX_ROWS", "0"),
        ("EXPORT_MAX_ROWS", "many"),
        ("QUERY_MAX_TIME_MS", "0"),
        ("EXPORT_MAX_TIME_MS", "soon"),
        ("TRUSTED_PROXY_COUNT", "-1"),
    ],
)
def test_invalid_numbers_are_rejected(prod_env, name, value):
    prod_env.setenv(name, value)
    with pytest.raises(ConfigError, match=name):
        ProdConfig.from_env()


def test_legacy_mongo_db_uri_still_works_with_a_warning(prod_env):
    prod_env.delenv("MONGO_URI")
    prod_env.setenv("mongo_db_uri", "mongodb://legacy.example.com:27017")
    with pytest.warns(FutureWarning, match="MONGO_URI"):
        config = ProdConfig.from_env()
    assert config.MONGO_URI == "mongodb://legacy.example.com:27017"


def test_test_config_keeps_its_own_engine_options(app):
    assert app.testing
    assert app.config["SQLALCHEMY_ENGINE_OPTIONS"] == {}
    with app.app_context():
        assert db.engine.url.drivername == "sqlite"
    assert app.test_client().get("/api/check-auth").status_code == 401


def test_create_app_twice_in_one_process(build_app):
    first = build_app(TestConfig)
    second = build_app(TestConfig)
    assert first is not second
    for app in (first, second):
        assert app.test_client().get("/api/check-auth").status_code == 401


def test_create_app_does_not_create_tables(build_app):
    app = build_app(TestConfig)
    with app.app_context():
        assert sa.inspect(db.engine).get_table_names() == []


def test_routes_import_without_an_app_context():
    result = subprocess.run(
        [sys.executable, "-c", "import app.routes.auth"],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


# --- HTTP behaviour ------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/admin/", "/api/admin/user/"])
def test_admin_interface_is_gone(client, path):
    response = client.get(path)
    assert response.status_code == 404
    assert response.is_json


def test_unknown_route_returns_json_404(client):
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.get_json()["error"] == "Not Found"


def test_login_required_returns_json_401(client):
    response = client.get("/api/user")
    assert response.status_code == 401
    assert response.get_json() == {"error": "Unauthorized", "message": "Authentication required."}


def test_wrong_method_returns_json_405_with_allow_header(client):
    response = client.get("/api/login")
    assert response.status_code == 405
    assert response.is_json
    assert "POST" in response.headers["Allow"]


def test_unhandled_exception_returns_generic_json_500(app, caplog):
    def explode():
        raise RuntimeError("database password is hunter2")

    app.add_url_rule("/api/test-explode", "test_explode", explode)

    response = app.test_client().get("/api/test-explode")

    assert response.status_code == 500
    assert response.get_json() == {"error": "Internal Server Error", "message": "An unexpected error occurred."}
    assert "hunter2" not in response.get_data(as_text=True)
    assert "hunter2" in caplog.text


def test_cors_allows_only_the_frontend_origin(client):
    allowed = client.get("/api/check-auth", headers={"Origin": TestConfig.FRONTEND_URL})
    assert allowed.headers["Access-Control-Allow-Origin"] == TestConfig.FRONTEND_URL
    assert allowed.headers["Access-Control-Allow-Credentials"] == "true"
    # The SPA reads the CSV file name when it runs on another origin (development).
    assert allowed.headers["Access-Control-Expose-Headers"] == "Content-Disposition"

    evil = client.get("/api/check-auth", headers={"Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in evil.headers


def test_cors_preflight(client):
    headers = {"Access-Control-Request-Method": "DELETE", "Access-Control-Request-Headers": "Content-Type"}
    allowed = client.options("/api/delete-account", headers={"Origin": TestConfig.FRONTEND_URL, **headers})
    assert allowed.headers["Access-Control-Allow-Origin"] == TestConfig.FRONTEND_URL
    methods = {m.strip() for m in allowed.headers["Access-Control-Allow-Methods"].split(",")}
    assert methods == {"GET", "POST", "DELETE"}

    evil = client.options("/api/delete-account", headers={"Origin": "https://evil.example", **headers})
    assert "Access-Control-Allow-Origin" not in evil.headers


class ProxiedConfig(TestConfig):
    TRUSTED_PROXY_COUNT = 1


@pytest.mark.parametrize(("config", "expected"), [(TestConfig, "127.0.0.1"), (ProxiedConfig, "203.0.113.7")])
def test_x_forwarded_for_is_trusted_only_behind_a_proxy(build_app, config, expected):
    app = build_app(config)
    app.add_url_rule("/api/test-remote-addr", "test_remote_addr", lambda: request.remote_addr)
    response = app.test_client().get("/api/test-remote-addr", headers={"X-Forwarded-For": "203.0.113.7"})
    assert response.get_data(as_text=True) == expected


class RateLimitedConfig(TestConfig):
    RATELIMIT_ENABLED = True


@limiter.limit("2/minute")
def _limited_view():
    return "ok"


@pytest.mark.parametrize(("config", "last_status"), [(TestConfig, 200), (RateLimitedConfig, 429)])
def test_rate_limit_is_json_and_off_in_tests(build_app, config, last_status):
    app = build_app(config)
    app.add_url_rule("/api/test-limited", "test_limited", _limited_view)
    client = app.test_client()
    statuses = [client.get("/api/test-limited").status_code for _ in range(3)]

    assert statuses == [200, 200, last_status]
    if last_status == 429:
        assert client.get("/api/test-limited").get_json()["error"] == "Too Many Requests"


# --- Mongo -----------------------------------------------------------------------------------


def test_mongo_client_is_created_lazily_and_reused(app, build_app, monkeypatch):
    import mongomock

    from app import extensions

    created, registered = [], []

    def fake_mongo_client(uri, **options):
        created.append((uri, options))
        return mongomock.MongoClient()

    monkeypatch.setattr(extensions, "_mongo_client", None)
    monkeypatch.setattr(extensions.pymongo, "MongoClient", fake_mongo_client)
    monkeypatch.setattr(extensions.atexit, "register", registered.append)
    app.config.update(MONGO_URI="mongodb://mongo.example.com:27017", MONGO_DB="cortex_test", MONGO_COLLECTION="images")

    build_app(TestConfig)
    assert created == []

    with app.app_context():
        first = get_mongo_collection()
        second = get_mongo_collection()

    assert len(created) == 1
    uri, options = created[0]
    assert uri == "mongodb://mongo.example.com:27017"
    # Fails well within gunicorn's 30 s worker timeout when MongoDB is unreachable.
    assert options == {"serverSelectionTimeoutMS": 5_000, "connectTimeoutMS": 5_000, "socketTimeoutMS": 20_000}
    assert len(registered) == 1
    assert (first.database.name, first.name) == ("cortex_test", "images")
    assert first.database.client is second.database.client


# --- models ----------------------------------------------------------------------------------


def test_new_users_get_no_privileges(app, make_user):
    user = make_user(email="someone@example.com")
    assert not user.superuser
    assert user.subscription_status is None


def test_passwords(app, user, google_user, user_password):
    assert user.password.startswith("pbkdf2:sha256:")
    assert user.check_password(user_password)
    assert not user.check_password("wrong password")
    assert google_user.password is None
    assert not google_user.check_password("")
    assert not google_user.check_password(user_password)


def test_session_token_is_part_of_the_login_id(app, make_user):
    first = make_user()
    second = make_user()
    assert len(first.session_token) >= 43
    assert first.session_token != second.session_token
    assert first.get_id() == f"{first.id}:{first.session_token}"

    old_id = first.get_id()
    first.rotate_session_token()
    assert first.get_id() != old_id
    assert first.get_id().startswith(f"{first.id}:")


def test_stripe_event_ids_are_unique(app):
    with app.app_context():
        db.session.add(StripeEvent(id="evt_1", created=1_700_000_000, type="invoice.payment_succeeded"))
        db.session.commit()
        db.session.add(StripeEvent(id="evt_1", created=1_700_000_001, type="invoice.payment_succeeded"))
        with pytest.raises(sa.exc.IntegrityError):
            db.session.commit()
        db.session.rollback()


# --- CLI ------------------------------------------------------------------------------------


def test_init_db_creates_the_tables(app):
    with app.app_context():
        db.drop_all()

    result = app.test_cli_runner().invoke(args=["init-db"])

    assert result.exit_code == 0, result.output
    with app.app_context():
        assert {"user", "stripe_event"} <= set(sa.inspect(db.engine).get_table_names())


def test_make_admin_sets_the_superuser_flag(app, make_user):
    target = make_user(email="a@b.c")

    result = app.test_cli_runner().invoke(args=["make-admin", " A@B.C "])

    assert result.exit_code == 0, result.output
    with app.app_context():
        assert db.session.get(User, target.id).superuser is True


def test_make_admin_unknown_email_fails(app):
    result = app.test_cli_runner().invoke(args=["make-admin", "nobody@example.com"])
    assert result.exit_code != 0
    assert "No user with email nobody@example.com" in result.output


# --- run.py ----------------------------------------------------------------------------------


@pytest.fixture
def run_py_calls(prod_env):
    """Records load_dotenv() and app.run() calls made by run.py; no local .env is read."""
    import dotenv

    calls = []
    prod_env.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: calls.append("load_dotenv"))
    prod_env.setattr(Flask, "run", lambda self, **kwargs: calls.append(kwargs))
    return calls


@pytest.mark.parametrize(("flask_debug", "expected"), [(None, False), ("0", False), ("true", False), ("1", True)])
def test_run_py_debug_only_with_flask_debug_1(prod_env, run_py_calls, flask_debug, expected):
    if flask_debug is not None:
        prod_env.setenv("FLASK_DEBUG", flask_debug)

    namespace = runpy.run_path(str(BACKEND_DIR / "run.py"), run_name="__main__")

    assert isinstance(namespace["app"], Flask)
    assert run_py_calls == ["load_dotenv", {"debug": expected}]


def test_run_py_as_wsgi_module_does_not_start_a_server(run_py_calls):
    namespace = runpy.run_path(str(BACKEND_DIR / "run.py"), run_name="run")
    assert isinstance(namespace["app"], Flask)
    assert run_py_calls == ["load_dotenv"]
