"""The filter language of POST /api/get-labeled-data (the request's "filter").

A small JSON language shaped like MongoDB queries, over an allowlist of fields
and operators. A filter is validated and translated into a MongoDB query here;
nothing the client sends reaches MongoDB as given. See docs/API.md for the
reference.

    node       := {} (top level only: no condition)
                | {"$and": [node, ...]} | {"$or": [node, ...]} | {"$not": node}
                | {"class": NAME, "count": cmp?, "max_box_area": cmp?}
                | {FIELD: cmp, ...}
    cmp        := value | {OP: value, ...}    OP in $eq $gt $gte $lt $lte $in
"""
import math
from datetime import datetime, timezone

from bson import ObjectId

MAX_DEPTH = 8  # nesting of $and / $or / $not
MAX_CONDITIONS = 64  # class and field conditions in one filter
MAX_IN_VALUES = 100

VOC_CLASSES = frozenset({
    "aeroplane", "bicycle", "bird", "boat", "bottle", "bus", "car", "cat", "chair", "cow",
    "diningtable", "dog", "horse", "motorbike", "person", "pottedplant", "sheep", "sofa", "train",
    "tvmonitor",
})
OPERATORS = ("$eq", "$gt", "$gte", "$lt", "$lte", "$in")
RANGE_OPERATORS = ("$gt", "$gte", "$lt", "$lte")

# Only documents the pipeline has fully processed can match a filter.
PROCESSED = {"object_detection": {"$type": "object"}, "label_quality_score": {"$type": "number"}}


class FilterError(ValueError):
    """An invalid filter; `path` names the offending part, e.g. filter.$and[1].count."""

    def __init__(self, path, problem):
        super().__init__(f"{path}: {problem}")
        self.path = path
        self.problem = problem


def _is_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an int too large for a float
        return False


def _number(minimum=None, maximum=None, integer=False):
    """A value check for numbers within [minimum, maximum]."""
    kind = "an integer" if integer else "a number"
    if minimum is not None and maximum is not None:
        rule = f"{kind} from {minimum} to {maximum}"
    else:
        rule = f"{kind} >= {minimum}"

    def check(value, path):
        ok = _is_number(value) and (not integer or float(value).is_integer())
        if not ok or (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
            raise FilterError(path, f"must be {rule}")
        return int(value) if integer else float(value)

    return check


def _collected_time(value, path):
    """An ISO 8601 date or date-time (UTC unless it has an offset) as the smallest ObjectId of that second."""
    moment = None
    if isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            pass
    if moment is None:
        raise FilterError(path, 'must be an ISO 8601 date or date-time, e.g. "2026-09-01" or "2026-09-01T12:00:00Z"')
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    # An ObjectId holds the time as unsigned 32-bit seconds since 1970.
    if not 0 <= moment.timestamp() < 2**32:
        raise FilterError(path, "must be between 1970 and 2106")
    return ObjectId.from_datetime(moment)


# field -> (stored field, value check, allowed operators)
FIELDS = {
    "width": ("width", _number(minimum=0), OPERATORS),
    "height": ("height", _number(minimum=0), OPERATORS),
    "label_quality": ("label_quality_score", _number(minimum=0, maximum=1), OPERATORS),
    "object_count": ("object_total", _number(minimum=0, integer=True), OPERATORS),
    # The ObjectId starts with the second the pipeline stored the image, so this uses the _id index.
    "collected": ("_id", _collected_time, RANGE_OPERATORS),
}
# class condition key -> (stored map, value check)
CLASS_FIELDS = {
    "count": ("object_counts", _number(minimum=1, integer=True)),
    "max_box_area": ("object_max_area", _number(minimum=0, maximum=1)),
}


class _Translator:
    def __init__(self):
        self.conditions = 0

    def _count_condition(self, path):
        self.conditions += 1
        if self.conditions > MAX_CONDITIONS:
            raise FilterError(path, f"a filter may hold at most {MAX_CONDITIONS} conditions")

    def comparison(self, value, check, operators, path):
        """cmp -> a MongoDB comparison document."""
        if not isinstance(value, dict):
            if "$eq" not in operators:
                raise FilterError(path, "must be an object of " + ", ".join(operators))
            return {"$eq": check(value, path)}
        if not value:
            raise FilterError(path, "must hold at least one of " + ", ".join(operators))
        result = {}
        for operator, operand in value.items():
            where = f"{path}.{operator}"
            if operator not in operators:
                raise FilterError(where, "unknown operator; use one of " + ", ".join(operators))
            if operator == "$in":
                if not isinstance(operand, list) or not 1 <= len(operand) <= MAX_IN_VALUES:
                    raise FilterError(where, f"must be a list of 1 to {MAX_IN_VALUES} values")
                result[operator] = [check(item, f"{where}[{i}]") for i, item in enumerate(operand)]
            else:
                result[operator] = check(operand, where)
        return result

    def node(self, node, path, depth):
        if not isinstance(node, dict):
            raise FilterError(path, "must be an object")
        if not node:
            raise FilterError(path, "must hold a condition")
        logical = [key for key in node if key in ("$and", "$or", "$not")]
        if logical:
            if len(node) != 1:
                raise FilterError(path, f"{logical[0]} must be the only key of its object")
            if depth >= MAX_DEPTH:
                raise FilterError(path, f"filters may nest at most {MAX_DEPTH} levels")
            key, value = logical[0], node[logical[0]]
            if key == "$not":
                return {"$nor": [self.node(value, f"{path}.$not", depth + 1)]}
            if not isinstance(value, list) or not value:
                raise FilterError(f"{path}.{key}", "must be a non-empty list of conditions")
            return {key: [self.node(item, f"{path}.{key}[{i}]", depth + 1) for i, item in enumerate(value)]}
        if "class" in node:
            return self.class_condition(node, path)
        return self.field_conditions(node, path)

    def class_condition(self, node, path):
        self._count_condition(path)
        name = node["class"]
        if not isinstance(name, str) or name not in VOC_CLASSES:
            raise FilterError(f"{path}.class", "must be a Pascal VOC class name")
        clauses = [{f"object_counts.{name}": {"$gte": 1}}]
        for key, value in node.items():
            if key == "class":
                continue
            if key not in CLASS_FIELDS:
                raise FilterError(f"{path}.{key}", "unknown key; a class condition takes class, count and max_box_area")
            stored, check = CLASS_FIELDS[key]
            clauses.append({f"{stored}.{name}": self.comparison(value, check, OPERATORS, f"{path}.{key}")})
        return clauses[0] if len(clauses) == 1 else {"$and": clauses}

    def field_conditions(self, node, path):
        clauses = []
        for key, value in node.items():
            where = f"{path}.{key}"
            if key not in FIELDS:
                raise FilterError(where, "unknown field; use one of class, " + ", ".join(FIELDS) + ", $and, $or, $not")
            self._count_condition(where)
            stored, check, operators = FIELDS[key]
            clauses.append({stored: self.comparison(value, check, operators, where)})
        return clauses[0] if len(clauses) == 1 else {"$and": clauses}


def translate(filter_, path="filter"):
    """Validate a filter and return the MongoDB $match document for it; raises FilterError."""
    if not isinstance(filter_, dict):
        raise FilterError(path, "must be an object")
    if not filter_:
        return dict(PROCESSED)
    return {"$and": [dict(PROCESSED), _Translator().node(filter_, path, 0)]}
