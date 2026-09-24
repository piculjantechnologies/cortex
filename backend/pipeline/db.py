"""MongoDB access for the pipeline (pymongo only, one client per process)."""
import atexit
import os

import pymongo
from pymongo.errors import OperationFailure

from .config import require_env

_client = None


def get_client():
    """The process-wide MongoClient for $MONGO_URI (deprecated fallback: $mongo_db_uri)."""
    global _client
    if _client is None:
        _client = pymongo.MongoClient(require_env("MONGO_URI", legacy="mongo_db_uri"))
        atexit.register(_client.close)
    return _client


def get_database():
    return get_client()[os.environ.get("MONGO_DB", "cortex")]


def get_collection():
    """The image collection the web app reads, with the indexes the pipeline relies on."""
    collection = get_database()[os.environ.get("MONGO_COLLECTION", "collection")]
    ensure_indexes(collection)
    return collection


def ensure_indexes(collection):
    """Unique url (makes ingest idempotent) plus the fields the web app filters on."""
    try:
        collection.create_index("url", unique=True)
    except OperationFailure as exc:
        raise SystemExit(
            f"cannot create the unique index on {collection.full_name}.url ({exc}); "
            "remove the documents with duplicate urls first"
        ) from exc
    for field in ("label_quality_score", "width", "height", "object_total"):
        collection.create_index(field)
    # Per-class counts and box areas (see object_stats): one wildcard index each.
    for field in ("object_counts.$**", "object_max_area.$**"):
        collection.create_index(field)
    # Images a user asked the web app to analyse: only those documents carry the field.
    collection.create_index("requested_at", sparse=True)


def draw(collection, match, size):
    """Up to `size` documents matching `match` for one round of a stage.

    Documents a user requested (requested_at, set by the web app's analyse
    endpoint) come first, oldest request first; the rest of the round is a
    random sample of the other matching documents.
    """
    requested = list(collection.find({**match, "requested_at": {"$exists": True}}).sort("requested_at", 1).limit(size))
    if len(requested) >= size:
        return requested
    others = collection.aggregate([
        {"$match": {**match, "requested_at": {"$exists": False}}},
        {"$sample": {"size": size - len(requested)}},
    ])
    return requested + list(others)


def has_requests(collection, match):
    """True when a requested document is waiting, so a stage can start a new round for it."""
    return collection.find_one({**match, "requested_at": {"$exists": True}}, {"_id": 1}) is not None
