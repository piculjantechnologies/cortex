"""The filter language: translation, limits and every rejection."""
from datetime import datetime, timezone

import pytest
from bson import ObjectId

from app.query_language import MAX_CONDITIONS, MAX_DEPTH, MAX_IN_VALUES, PROCESSED, FilterError, translate


def inner(filter_):
    """The translated filter without the fixed 'fully processed' part."""
    match = translate(filter_)
    assert match["$and"][0] == PROCESSED
    return match["$and"][1]


def test_empty_filter_matches_every_processed_document():
    assert translate({}) == PROCESSED
    assert PROCESSED == {"object_detection": {"$type": "object"}, "label_quality_score": {"$type": "number"}}


def test_class_presence_count_and_box_area():
    assert inner({"class": "dog"}) == {"object_counts.dog": {"$gte": 1}}
    assert inner({"class": "person", "count": {"$gte": 2, "$lte": 4}, "max_box_area": {"$gte": 0.1}}) == {
        "$and": [
            {"object_counts.person": {"$gte": 1}},
            {"object_counts.person": {"$gte": 2, "$lte": 4}},
            {"object_max_area.person": {"$gte": 0.1}},
        ]
    }
    assert inner({"class": "cat", "count": 3}) == {
        "$and": [{"object_counts.cat": {"$gte": 1}}, {"object_counts.cat": {"$eq": 3}}]
    }


def test_fields_map_to_the_stored_fields():
    assert inner({"width": {"$gte": 640}, "height": {"$lt": 2000}}) == {
        "$and": [{"width": {"$gte": 640.0}}, {"height": {"$lt": 2000.0}}]
    }
    assert inner({"label_quality": {"$gte": 0.8}}) == {"label_quality_score": {"$gte": 0.8}}
    assert inner({"object_count": {"$in": [1, 2]}}) == {"object_total": {"$in": [1, 2]}}


def test_collected_compares_the_object_id_time():
    moment = datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert inner({"collected": {"$gte": "2026-09-01"}}) == {"_id": {"$gte": ObjectId.from_datetime(moment)}}
    assert inner({"collected": {"$lt": "2026-09-01T02:00:00+02:00"}}) == {
        "_id": {"$lt": ObjectId.from_datetime(moment)}
    }


def test_logic_nodes():
    assert inner({"$and": [{"class": "cat"}, {"$or": [{"class": "dog"}, {"class": "car"}]}]}) == {
        "$and": [
            {"object_counts.cat": {"$gte": 1}},
            {"$or": [{"object_counts.dog": {"$gte": 1}}, {"object_counts.car": {"$gte": 1}}]},
        ]
    }
    assert inner({"$not": {"class": "person"}}) == {"$nor": [{"object_counts.person": {"$gte": 1}}]}


def test_the_limits_themselves_are_accepted():
    nested = {"class": "dog"}
    for _ in range(MAX_DEPTH):
        nested = {"$not": nested}
    translate(nested)
    translate({"$and": [{"class": "dog"}] * MAX_CONDITIONS})
    translate({"width": {"$in": list(range(MAX_IN_VALUES))}})


@pytest.mark.parametrize(
    ("filter_", "path", "problem"),
    [
        ([], "filter", "must be an object"),
        ({"$and": [{}]}, "filter.$and[0]", "must hold a condition"),
        ({"$and": []}, "filter.$and", "non-empty list"),
        ({"$or": {"class": "dog"}}, "filter.$or", "non-empty list"),
        ({"$not": [{"class": "dog"}]}, "filter.$not", "must be an object"),
        ({"$and": [{"class": "dog"}], "width": 5}, "filter", "only key"),
        ({"$where": "sleep(1000)"}, "filter.$where", "unknown field"),
        ({"$expr": {"$gt": ["$width", 1]}}, "filter.$expr", "unknown field"),
        ({"url": {"$regex": ".*"}}, "filter.url", "unknown field"),
        ({"object_detection.dog": {"$exists": True}}, "filter.object_detection.dog", "unknown field"),
        ({"width": {"$regex": "1"}}, "filter.width.$regex", "unknown operator"),
        ({"width": {"$ne": 1}}, "filter.width.$ne", "unknown operator"),
        ({"width": {}}, "filter.width", "at least one"),
        ({"width": -1}, "filter.width", "number >= 0"),
        ({"width": "640"}, "filter.width", "number >= 0"),
        ({"width": True}, "filter.width", "number >= 0"),
        ({"width": 10**400}, "filter.width", "number >= 0"),
        ({"width": {"$in": []}}, "filter.width.$in", "list of 1 to"),
        ({"width": {"$in": list(range(MAX_IN_VALUES + 1))}}, "filter.width.$in", "list of 1 to"),
        ({"width": {"$in": [1, "x"]}}, "filter.width.$in[1]", "number >= 0"),
        ({"label_quality": 1.5}, "filter.label_quality", "from 0 to 1"),
        ({"object_count": 1.5}, "filter.object_count", "an integer"),
        ({"collected": "2026-09-01"}, "filter.collected", "must be an object"),
        ({"collected": {"$eq": "2026-09-01"}}, "filter.collected.$eq", "unknown operator"),
        ({"collected": {"$gte": "yesterday"}}, "filter.collected.$gte", "ISO 8601"),
        ({"collected": {"$gte": 20260901}}, "filter.collected.$gte", "ISO 8601"),
        ({"collected": {"$gte": "1969-12-31"}}, "filter.collected.$gte", "between 1970 and 2106"),
        ({"collected": {"$lt": "2200-01-01"}}, "filter.collected.$lt", "between 1970 and 2106"),
        ({"class": "unicorn"}, "filter.class", "Pascal VOC"),
        ({"class": ["dog"]}, "filter.class", "Pascal VOC"),
        ({"class": "dog", "count": 0}, "filter.count", "integer >= 1"),
        ({"class": "dog", "max_box_area": 2}, "filter.max_box_area", "from 0 to 1"),
        ({"class": "dog", "width": 5}, "filter.width", "a class condition takes"),
        ({"$or": [{"class": "dog"}, {"$not": {"class": "x"}}]}, "filter.$or[1].$not.class", "Pascal VOC"),
    ],
)
def test_rejections_name_the_path(filter_, path, problem):
    with pytest.raises(FilterError) as info:
        translate(filter_)
    assert info.value.path == path
    assert problem in info.value.problem


def test_too_deep_is_rejected():
    nested = {"class": "dog"}
    for _ in range(MAX_DEPTH + 1):
        nested = {"$not": nested}
    with pytest.raises(FilterError, match="nest at most"):
        translate(nested)


def test_too_many_conditions_are_rejected():
    with pytest.raises(FilterError, match=f"at most {MAX_CONDITIONS} conditions"):
        translate({"$or": [{"class": "dog"}] * (MAX_CONDITIONS + 1)})
