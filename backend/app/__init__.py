from flask import Flask, current_app, json, jsonify, request
from flask_cors import CORS
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from .cli import register_commands
from .config import ProdConfig
from .extensions import db, get_mongo_collection, limiter, login_manager

__all__ = ["create_app", "db", "get_mongo_collection", "limiter", "login_manager"]


def create_app(config_object=None):
    """Build the app; without an argument the configuration comes from the environment."""
    app = Flask(__name__)
    app.config.from_object(config_object if config_object is not None else ProdConfig.from_env())

    proxy_count = app.config.get("TRUSTED_PROXY_COUNT", 0)
    if proxy_count:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=proxy_count)

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.unauthorized_handler(_unauthorized)
    limiter.init_app(app)
    CORS(
        app,
        origins=[app.config["FRONTEND_URL"]],
        supports_credentials=True,
        methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type"],
        expose_headers=["Content-Disposition"],  # the CSV export's file name, read by the SPA
    )

    app.register_error_handler(HTTPException, _handle_http_exception)
    app.register_error_handler(Exception, _handle_exception)
    register_commands(app)

    # Imported here rather than at the top to avoid circular imports
    from .routes.auth import bp as auth_bp
    from .routes.cortex import bp as cortex_bp
    from .routes.images import bp as images_bp
    from .routes.stripe import bp as stripe_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(stripe_bp)
    app.register_blueprint(cortex_bp)
    app.register_blueprint(images_bp)

    return app


def _unauthorized():
    return jsonify(error="Unauthorized", message="Authentication required."), 401


def _handle_http_exception(error):
    # Keep the status code and headers (Allow, Retry-After, ...) but send JSON instead of HTML.
    response = error.get_response()
    response.data = json.dumps({"error": error.name, "message": error.description})
    response.content_type = "application/json"
    return response


def _handle_exception(error):
    # Log the details; never send them to the client.
    current_app.logger.error("Unhandled exception on %s %s", request.method, request.path, exc_info=error)
    return jsonify(error="Internal Server Error", message="An unexpected error occurred."), 500
