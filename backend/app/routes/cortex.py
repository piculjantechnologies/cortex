import csv
import io
import json
import math

from flask import Blueprint, Response, current_app, jsonify, request, stream_with_context
from pymongo.errors import ExecutionTimeout, PyMongoError

from ..decorators import subscription_required
from ..extensions import get_mongo_collection
from ..query_language import FilterError, translate

# Blueprint setup
bp = Blueprint("cortex", __name__)

DEFAULT_PER_PAGE = 25
MAX_PER_PAGE = 100
# Keeps the $skip well inside MongoDB's 64-bit integers.
MAX_PAGE = 1_000_000_000
# The 20 Pascal VOC classes the detection stage stores under object_detection.
VOC_CLASSES = frozenset({
    "aeroplane", "bicycle", "bird", "boat", "bottle", "bus", "car", "cat", "chair", "cow",
    "diningtable", "dog", "horse", "motorbike", "person", "pottedplant", "sheep", "sofa", "train",
    "tvmonitor",
})
# The markers the detection stage writes into object_detection instead of boxes.
ERROR_SENTINELS = ["error: broken image", "error: too large"]
CSV_HEADERS = ["_id", "url", "width", "height", "hash", "object_detection", "label_quality_score"]
# How include_classes combine: every listed class, or at least one of them.
INCLUDE_MODES = ("all", "any")
# Result orders; _id breaks ties, so pages never overlap.
SORTS = {
    "id": {"_id": 1},
    "quality": {"label_quality_score": -1, "_id": 1},
    "newest": {"_id": -1},
}


class _ValidationError(Exception):
    def __init__(self, fields):
        super().__init__(fields)
        self.fields = fields


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an int too large for a float
        return False


def _parse_request(body):
    """Validate the request body; returns (fetch_all, page, per_page, match, sort) or raises _ValidationError."""
    if not isinstance(body, dict):
        raise _ValidationError({"body": "must be a JSON object"})
    query = body.get("query", {})
    if not isinstance(query, dict):
        raise _ValidationError({"query": "must be an object"})

    errors = {}
    filter_match = None
    if "filter" in body:
        if "query" in body:
            errors["query"] = "cannot be combined with filter"
        try:
            filter_match = translate(body["filter"])
        except FilterError as e:
            errors[e.path] = e.problem
    fetch_all = body.get("fetch_all", False)
    if not isinstance(fetch_all, bool):
        errors["fetch_all"] = "must be true or false"
    page = body.get("page", 1)
    if not _is_int(page) or not 1 <= page <= MAX_PAGE:
        errors["page"] = f"must be an integer from 1 to {MAX_PAGE}"
    per_page = body.get("per_page", DEFAULT_PER_PAGE)
    if not _is_int(per_page) or not 1 <= per_page <= MAX_PER_PAGE:
        errors["per_page"] = f"must be an integer from 1 to {MAX_PER_PAGE}"

    min_width = query.get("min_width", 100)
    if not _is_number(min_width) or min_width < 0:
        errors["min_width"] = "must be a number >= 0"
    min_height = query.get("min_height", 0)
    if not _is_number(min_height) or min_height < 0:
        errors["min_height"] = "must be a number >= 0"
    label_quality_score = query.get("label_quality_score", 50)
    if not _is_number(label_quality_score) or not 0 <= label_quality_score <= 100:
        errors["label_quality_score"] = "must be a number from 0 to 100"

    classes = {}
    for field in ("include_classes", "exclude_classes"):
        value = query.get(field, [])
        if not isinstance(value, list) or not all(isinstance(c, str) and c in VOC_CLASSES for c in value):
            errors[field] = "must be a list of Pascal VOC class names"
        classes[field] = value
    include_mode = query.get("include_mode", "all")
    if include_mode not in INCLUDE_MODES:
        errors["include_mode"] = "must be one of: " + ", ".join(INCLUDE_MODES)
    sort = body.get("sort", "id")
    if not isinstance(sort, str) or sort not in SORTS:
        errors["sort"] = "must be one of: " + ", ".join(SORTS)

    if errors:
        raise _ValidationError(errors)
    if filter_match is not None:
        return fetch_all, page, per_page, filter_match, SORTS[sort]

    match_conditions = {
        # Exclude images the detection stage could not process
        "object_detection": {"$nin": ERROR_SENTINELS},
    }
    if classes["include_classes"]:
        match_conditions["$and" if include_mode == "all" else "$or"] = [
            {"object_detection." + cls: {"$exists": True}} for cls in classes["include_classes"]
        ]
    if classes["exclude_classes"]:
        match_conditions["$nor"] = [
            {"object_detection." + cls: {"$exists": True}} for cls in classes["exclude_classes"]
        ]
    match_conditions["width"] = {"$gte": float(min_width)}
    match_conditions["height"] = {"$gte": float(min_height)}
    match_conditions["label_quality_score"] = {"$gte": label_quality_score / 100}

    return fetch_all, page, per_page, match_conditions, SORTS[sort]


def _csv_safe(value):
    """Prefix text that a spreadsheet would run as a formula with an apostrophe."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _csv_row(sample):
    return [_csv_safe(cell) for cell in (
        "PT::" + str(sample.get("_id")),
        sample.get("url"),
        sample.get("width"),
        sample.get("height"),
        sample.get("hash"),
        json.dumps(sample.get("object_detection", {})),
        sample.get("label_quality_score"),
    )]


def _mongo_unavailable():
    current_app.logger.exception("MongoDB query failed")
    return jsonify(error="Bad Gateway", message="The image database is unavailable."), 502


def _query_timed_out():
    current_app.logger.info("A search ran past QUERY_MAX_TIME_MS")
    return jsonify(
        error="Gateway Timeout",
        message="The search took too long. Narrow it, for example with a class or a higher label quality.",
    ), 504


def _export_csv(collection, match_conditions, sort):
    """Stream every matching row (up to EXPORT_MAX_ROWS) as CSV, one line at a time."""
    pipeline = [
        {"$match": match_conditions},
        {"$sort": sort},
        {"$limit": current_app.config["EXPORT_MAX_ROWS"]},
    ]
    try:
        cursor = collection.aggregate(
            pipeline, batchSize=1000, maxTimeMS=current_app.config["EXPORT_MAX_TIME_MS"]
        )
    except ExecutionTimeout:
        return _query_timed_out()
    except PyMongoError:
        return _mongo_unavailable()

    def generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer)

        def line(row):
            writer.writerow(row)
            text = buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()
            return text

        try:
            yield line(CSV_HEADERS)
            for sample in cursor:
                yield line(_csv_row(sample))
        except PyMongoError:
            # The status line is already sent: log and abort, so the client sees a failed download.
            current_app.logger.exception("MongoDB cursor failed during the CSV export")
            raise
        finally:
            cursor.close()

    return Response(
        stream_with_context(generate()),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=data.csv"},
    )


@bp.route("/api/get-labeled-data", methods=["POST"])
@subscription_required
def get_labeled_data():
    try:
        fetch_all, page, per_page, match_conditions, sort = _parse_request(request.get_json(silent=True))
    except _ValidationError as e:
        message = "; ".join(f"{field}: {problem}" for field, problem in e.fields.items())
        return jsonify(error="Bad Request", message=message, fields=e.fields), 400

    try:
        # The first call builds the client, which can already fail (e.g. a mongodb+srv DNS lookup).
        collection_cortex = get_mongo_collection()
    except PyMongoError:
        return _mongo_unavailable()

    if fetch_all:
        return _export_csv(collection_cortex, match_conditions, sort)

    skip = (page - 1) * per_page
    query_pipeline = [
        {"$match": match_conditions},
        {"$sort": sort},
        {"$skip": skip},
        {"$limit": per_page},
    ]
    count_pipeline = [{"$match": match_conditions}, {"$count": "total"}]
    max_time_ms = current_app.config["QUERY_MAX_TIME_MS"]
    try:
        samples = list(collection_cortex.aggregate(query_pipeline, maxTimeMS=max_time_ms))
        count_result = list(collection_cortex.aggregate(count_pipeline, maxTimeMS=max_time_ms))
    except ExecutionTimeout:
        return _query_timed_out()
    except PyMongoError:
        return _mongo_unavailable()

    total_documents = count_result[0]["total"] if count_result else 0
    total_pages = (total_documents + per_page - 1) // per_page

    output = []
    for sample in samples:
        sample["_id"] = "PT::" + str(sample["_id"])
        output.append(sample)

    # Prepare the response
    res = {
        "output": output,
        "length": total_documents,
        "current_page": page,
        "total_pages": total_pages,
        "has_next_page": page < total_pages,
    }

    return jsonify(res)
