"""Per-image lookups and analysis requests, used by the browser extension.

The extension asks what Cortex knows about the images of the page the user is
viewing, and can ask for an image Cortex does not have yet to be analysed. An
analysis request only queues the URL in the image collection with a
`requested_at` time; the pipeline's detection and scoring stages take requested
documents first. The web app itself never downloads the image.
"""
from datetime import datetime, timezone
from urllib.parse import urlsplit

from flask import Blueprint, current_app, jsonify, request
from flask_limiter.util import get_remote_address
from flask_login import current_user
from pymongo.errors import DuplicateKeyError, ExecutionTimeout, PyMongoError
from url_normalize import url_normalize

from ..decorators import subscription_required
from ..extensions import get_mongo_collection, limiter
from .cortex import _mongo_unavailable, _query_timed_out

bp = Blueprint("images", __name__)

MAX_URLS = 50
MAX_URL_LENGTH = 2048
FIELDS = {
    "url": 1, "object_detection": 1, "width": 1, "height": 1,
    "label_quality_score": 1, "label_quality_error": 1, "requested_at": 1,
}


def _user_key():
    """Rate-limit key: the logged-in user, or the client address before login is checked."""
    if current_user.is_authenticated:
        return f"user:{current_user.id}"
    return get_remote_address()


def normalise_url(url):
    """The collection's form of an http(s) image URL, or None when the URL cannot be stored."""
    if not isinstance(url, str) or len(url) > MAX_URL_LENGTH:
        return None
    url = url.strip()
    # url_normalize would turn a scheme-less string (even "javascript:...") into an https URL.
    if not url.lower().startswith(("http://", "https://")):
        return None
    try:
        normalised = url_normalize(url)
    except Exception:  # url_normalize raises assorted errors for malformed input
        return None
    parts = urlsplit(normalised)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or len(normalised) > MAX_URL_LENGTH
        or any(character.isspace() for character in normalised)
    ):
        return None
    return normalised


def describe(document):
    """What Cortex knows about one image, for the extension."""
    if document is None:
        return {"status": "unknown"}
    result = {"id": "PT::" + str(document["_id"]), "requested": "requested_at" in document}
    detection = document.get("object_detection")
    if detection is None:
        return {**result, "status": "queued"}
    if isinstance(detection, str):
        return {**result, "status": "failed", "reason": detection.removeprefix("error: ")}
    result.update(width=document.get("width"), height=document.get("height"), object_detection=detection)
    if "label_quality_score" not in document:
        return {**result, "status": "scoring"}
    result.update(status="done", label_quality_score=document["label_quality_score"])
    if document["label_quality_score"] is None:
        result["label_quality_error"] = document.get("label_quality_error")
    return result


def _bad_request(field, problem):
    return jsonify(error="Bad Request", message=f"{field}: {problem}", fields={field: problem}), 400


@bp.route("/api/images/lookup", methods=["POST"])
@limiter.limit("120/minute", key_func=_user_key)
@subscription_required
def lookup():
    """{"urls": [...]} -> {"results": {url: description}} for up to MAX_URLS image URLs."""
    body = request.get_json(silent=True)
    urls = body.get("urls") if isinstance(body, dict) else None
    if not isinstance(urls, list) or not 1 <= len(urls) <= MAX_URLS or not all(isinstance(u, str) for u in urls):
        return _bad_request("urls", f"must be a list of 1 to {MAX_URLS} URLs")

    normalised = {url: normalise_url(url) for url in urls}
    wanted = sorted({n for n in normalised.values() if n})
    try:
        documents = {
            document["url"]: document
            for document in get_mongo_collection().find(
                {"url": {"$in": wanted}}, FIELDS, max_time_ms=current_app.config["QUERY_MAX_TIME_MS"]
            )
        } if wanted else {}
    except ExecutionTimeout:
        return _query_timed_out()
    except PyMongoError:
        return _mongo_unavailable()

    results = {
        url: describe(documents.get(n)) if n else {"status": "invalid"}
        for url, n in normalised.items()
    }
    return jsonify(results=results)


@bp.route("/api/images/analyse", methods=["POST"])
@limiter.limit("10/minute;200/day", key_func=_user_key)
@subscription_required
def analyse():
    """{"url": ...} -> queue the image for detection and scoring, ahead of the crawl's images.

    202 with the queued state for an image that still has to be processed, 200
    with its description when Cortex already has the result.
    """
    body = request.get_json(silent=True)
    url = normalise_url(body.get("url") if isinstance(body, dict) else None)
    if url is None:
        return _bad_request("url", f"must be an http(s) URL of at most {MAX_URL_LENGTH} characters")

    now = datetime.now(timezone.utc)
    try:
        collection = get_mongo_collection()
        document = collection.find_one({"url": url}, FIELDS)
        if document is not None and document.get("object_detection") is not None and (
            isinstance(document["object_detection"], str) or "label_quality_score" in document
        ):
            return jsonify(url=url, **describe(document)), 200
        # $min keeps the first request time, so repeated clicks do not move an image back in the queue.
        update = {
            "$setOnInsert": {"url": url, "datetime": str(now.replace(tzinfo=None)), "source": "extension"},
            "$min": {"requested_at": now},
        }
        try:
            collection.update_one({"url": url}, update, upsert=True)
        except DuplicateKeyError:  # another request inserted the same URL in between
            collection.update_one({"url": url}, {"$min": {"requested_at": now}})
        document = collection.find_one({"url": url}, FIELDS)
    except PyMongoError:
        return _mongo_unavailable()
    return jsonify(url=url, **describe(document)), 202
