"""Extension instances shared by the app factory, the models and the routes."""

import atexit
import threading

import pymongo
from flask import current_app
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

login_manager = LoginManager()
login_manager.session_protection = "basic"

# Storage and on/off switch come from RATELIMIT_STORAGE_URI and RATELIMIT_ENABLED.
limiter = Limiter(key_func=get_remote_address)

_mongo_client = None
_mongo_lock = threading.Lock()

# pymongo's defaults (30 s to find a server, no reply timeout) reach gunicorn's 30 s worker timeout,
# so an unreachable MongoDB would get the worker killed instead of answering 502. The socket
# timeout applies to each reply (batch), not to a whole streamed CSV export.
MONGO_CLIENT_OPTIONS = {
    "serverSelectionTimeoutMS": 5_000,
    "connectTimeoutMS": 5_000,
    "socketTimeoutMS": 20_000,
}


def get_mongo_client():
    """Return this process's MongoClient, creating it on first use.

    It is never created in create_app(): under ``gunicorn --preload`` the factory
    runs in the master before the workers fork, and a MongoClient is not fork-safe.
    """
    global _mongo_client
    with _mongo_lock:
        if _mongo_client is None:
            _mongo_client = pymongo.MongoClient(current_app.config["MONGO_URI"], **MONGO_CLIENT_OPTIONS)
            atexit.register(_mongo_client.close)
        return _mongo_client


def get_mongo_collection():
    config = current_app.config
    return get_mongo_client()[config["MONGO_DB"]][config["MONGO_COLLECTION"]]
