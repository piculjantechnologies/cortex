import csv
import io
import json
import math

import pytest
from pymongo.errors import ConfigurationError, ExecutionTimeout, OperationFailure, PyMongoError

from app.routes.cortex import MAX_PAGE, _csv_safe

URL = "/api/get-labeled-data"
MATCHING_DOCS = 30  # fake_mongo seeds 30 matching documents and 2 broken ones
CSV_HEADER = ["_id", "url", "width", "height", "hash", "object_detection", "label_quality_score"]
DEFAULT_MATCH = {
    "object_detection": {"$nin": ["error: broken image", "error: too large"]},
    "width": {"$gte": 100.0},
    "height": {"$gte": 0.0},
    "label_quality_score": {"$gte": 0.5},
}


@pytest.fixture
def client(login_as, subscriber):
    """A client logged in as an active subscriber (overrides the anonymous client)."""
    return login_as(subscriber)


def csv_rows(response):
    return list(csv.reader(io.StringIO(response.get_data(as_text=True))))


# --- access --------------------------------------------------------------------------------------


def test_anonymous_is_401(app, fake_mongo):
    assert app.test_client().post(URL, json={}).status_code == 401
    assert fake_mongo.pipelines == []


@pytest.mark.parametrize("status", [None, "canceled", "past_due", "unpaid", "incomplete"])
def test_accounts_without_access_are_403(login_as, make_user, fake_mongo, status):
    response = login_as(make_user(subscription_status=status)).post(URL, json={})

    assert response.status_code == 403
    assert response.get_json() == {"error": "Forbidden", "message": "An active subscription is required."}
    assert fake_mongo.pipelines == []


@pytest.mark.parametrize("columns", [{"subscription_status": "active"}, {"subscription_status": "trialing"},
                                     {"superuser": True}])
def test_subscribers_and_complimentary_accounts_get_data(login_as, make_user, fake_mongo, columns):
    assert login_as(make_user(**columns)).post(URL, json={}).status_code == 200


# --- JSON pages ----------------------------------------------------------------------------------


def test_default_query_pipeline(client, fake_mongo):
    response = client.post(URL, json={})

    assert response.status_code == 200
    page_pipeline, count_pipeline = fake_mongo.pipelines
    assert page_pipeline == [
        {"$match": DEFAULT_MATCH},
        {"$sort": {"_id": 1}},
        {"$skip": 0},
        {"$limit": 25},
    ]
    assert count_pipeline == [{"$match": DEFAULT_MATCH}, {"$count": "total"}]


def test_first_page(client, fake_mongo):
    data = client.post(URL, json={"page": 1, "fetch_all": False, "query": {}}).get_json()

    assert set(data) == {"output", "length", "current_page", "total_pages", "has_next_page"}
    assert len(data["output"]) == 25
    assert data["length"] == MATCHING_DOCS
    assert data["current_page"] == 1
    assert data["total_pages"] == math.ceil(MATCHING_DOCS / 25) == 2
    assert data["has_next_page"] is True
    assert all(doc["_id"].startswith("PT::") for doc in data["output"])
    ids = [doc["_id"] for doc in data["output"]]
    assert ids == sorted(ids)


def test_second_page_continues_in_id_order(client, fake_mongo):
    first = client.post(URL, json={"page": 1}).get_json()["output"]
    second = client.post(URL, json={"page": 2}).get_json()

    assert fake_mongo.pipelines[2][2] == {"$skip": 25}
    assert len(second["output"]) == MATCHING_DOCS - 25
    assert second["has_next_page"] is False
    assert not {doc["_id"] for doc in first} & {doc["_id"] for doc in second["output"]}
    assert first[-1]["_id"] < second["output"][0]["_id"]


def test_per_page(client, fake_mongo):
    data = client.post(URL, json={"page": 3, "per_page": 7}).get_json()

    assert fake_mongo.pipelines[0][2:] == [{"$skip": 14}, {"$limit": 7}]
    assert data["total_pages"] == math.ceil(MATCHING_DOCS / 7)
    assert len(data["output"]) == 7


def test_class_filters(client, fake_mongo):
    query = {"include_classes": ["dog"], "exclude_classes": ["person", "car"]}

    data = client.post(URL, json={"query": query}).get_json()

    match = fake_mongo.pipelines[0][0]["$match"]
    assert match["$and"] == [{"object_detection.dog": {"$exists": True}}]
    assert match["$nor"] == [
        {"object_detection.person": {"$exists": True}},
        {"object_detection.car": {"$exists": True}},
    ]
    assert data["length"] == 10  # every third seeded document has a dog
    assert all("dog" in doc["object_detection"] for doc in data["output"])


def test_include_mode_any_matches_at_least_one_class(client, fake_mongo):
    query = {"include_classes": ["dog", "car"], "include_mode": "any"}

    data = client.post(URL, json={"query": query}).get_json()

    match = fake_mongo.pipelines[0][0]["$match"]
    assert "$and" not in match
    assert match["$or"] == [
        {"object_detection.dog": {"$exists": True}},
        {"object_detection.car": {"$exists": True}},
    ]
    assert data["length"] == 20  # a dog or a car in two of every three seeded documents


def test_include_mode_all_is_the_default(client, fake_mongo):
    client.post(URL, json={"query": {"include_classes": ["dog", "car"]}})
    client.post(URL, json={"query": {"include_classes": ["dog", "car"], "include_mode": "all"}})

    first, _, second, _ = fake_mongo.pipelines
    assert first == second
    assert first[0]["$match"]["$and"] == [
        {"object_detection.dog": {"$exists": True}},
        {"object_detection.car": {"$exists": True}},
    ]


@pytest.mark.parametrize(
    ("sort", "stage"),
    [
        ("id", {"_id": 1}),
        ("quality", {"label_quality_score": -1, "_id": 1}),
        ("newest", {"_id": -1}),
    ],
)
def test_sort(client, fake_mongo, sort, stage):
    data = client.post(URL, json={"sort": sort}).get_json()

    assert fake_mongo.pipelines[0][1] == {"$sort": stage}
    assert data["length"] == MATCHING_DOCS


def test_sort_by_quality_puts_the_best_labels_first(client, fake_mongo):
    output = client.post(URL, json={"sort": "quality", "per_page": 100}).get_json()["output"]

    scores = [doc["label_quality_score"] for doc in output]
    assert scores == sorted(scores, reverse=True)


def test_csv_export_uses_the_sort(client, fake_mongo):
    client.post(URL, json={"fetch_all": True, "sort": "quality"})

    assert fake_mongo.pipelines[0][1] == {"$sort": {"label_quality_score": -1, "_id": 1}}


# --- filter language ------------------------------------------------------------------------------


def test_filter_is_translated_and_applied(client, fake_mongo):
    fake_mongo.insert_one({
        "url": "https://images.example.com/crowd.jpg",
        "width": 800,
        "height": 600,
        "object_detection": {"person": [[0, 0, 0.5, 0.5]] * 3},
        "object_counts": {"person": 3},
        "object_max_area": {"person": 0.25},
        "object_total": 3,
        "label_quality_score": 0.95,
    })
    filter_ = {"$and": [{"class": "person", "count": {"$gte": 2}}, {"$not": {"class": "dog"}}]}

    data = client.post(URL, json={"filter": filter_, "sort": "quality"}).get_json()

    assert data["length"] == 1
    assert data["output"][0]["url"] == "https://images.example.com/crowd.jpg"
    match = fake_mongo.pipelines[0][0]["$match"]
    assert match["$and"][0] == {"object_detection": {"$type": "object"}, "label_quality_score": {"$type": "number"}}


def test_filter_or_and_numbers(client, fake_mongo):
    filter_ = {"$and": [{"$or": [{"class": "dog"}, {"class": "car"}]}, {"label_quality": {"$gte": 0.7}}]}

    output = client.post(URL, json={"filter": filter_}).get_json()

    # dog or car in two of every three seeded documents; scores 0.5 + i / 100 reach 0.7 from i = 20
    assert output["length"] == 7
    assert all(set(doc["object_detection"]) & {"dog", "car"} for doc in output["output"])


def test_empty_filter_returns_every_processed_document(client, fake_mongo):
    data = client.post(URL, json={"filter": {}}).get_json()
    assert data["length"] == MATCHING_DOCS  # the broken documents never match


def test_filter_csv_export(client, fake_mongo):
    rows = csv_rows(client.post(URL, json={"fetch_all": True, "filter": {"class": "car"}}))
    assert len(rows) == 1 + 10


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"filter": [], "sort": "id"}, "filter"),
        ({"filter": {"$where": "1"}}, "filter.$where"),
        ({"filter": {"class": "dog", "count": 0}}, "filter.count"),
        ({"filter": {"class": "dog"}, "query": {}}, "query"),
    ],
)
def test_invalid_filter_is_400_with_its_path(client, fake_mongo, body, field):
    response = client.post(URL, json=body)

    assert response.status_code == 400
    assert field in response.get_json()["fields"]
    assert fake_mongo.pipelines == []


@pytest.mark.parametrize("fetch_all", [False, True])
def test_queries_carry_a_time_limit(app, client, fake_mongo, fetch_all):
    app.config["QUERY_MAX_TIME_MS"] = 1234
    app.config["EXPORT_MAX_TIME_MS"] = 5678

    client.post(URL, json={"fetch_all": fetch_all}).get_data()

    expected = 5678 if fetch_all else 1234
    assert fake_mongo.options and all(o["maxTimeMS"] == expected for o in fake_mongo.options)


@pytest.mark.parametrize("fetch_all", [False, True])
def test_timeout_is_504_with_advice(client, fake_mongo, monkeypatch, fetch_all):
    def slow(*args, **kwargs):
        raise ExecutionTimeout("operation exceeded time limit")

    monkeypatch.setattr(fake_mongo, "aggregate", slow)

    response = client.post(URL, json={"fetch_all": fetch_all})

    assert response.status_code == 504
    assert "Narrow it" in response.get_json()["message"]


def test_numeric_filters(client, fake_mongo):
    query = {"min_width": 0, "min_height": 480.5, "label_quality_score": 70}

    data = client.post(URL, json={"query": query}).get_json()

    match = fake_mongo.pipelines[0][0]["$match"]
    assert (match["width"], match["height"], match["label_quality_score"]) == (
        {"$gte": 0.0}, {"$gte": 480.5}, {"$gte": 0.7}
    )
    assert data["length"] == 0  # the seeded images are 480 px high


def test_unknown_keys_are_ignored(client, fake_mongo):
    # The old frontend sent a `source` filter that the backend never applied.
    response = client.post(URL, json={"query": {"source": "commoncrawl"}, "extra": 1})
    assert response.status_code == 200
    assert fake_mongo.pipelines[0][0]["$match"] == DEFAULT_MATCH


@pytest.mark.parametrize(
    "body",
    [
        {"page": 1, "per_page": 100},
        {"page": MAX_PAGE, "per_page": 100},
        {"query": {"min_width": 10**300}},
        {"query": {"label_quality_score": 0, "min_width": 0, "min_height": 0}},
        {"query": {"label_quality_score": 100, "min_width": 12.5}},
        {"query": {"include_classes": [], "exclude_classes": ["tvmonitor", "aeroplane"]}},
    ],
)
def test_boundary_values_are_accepted(client, fake_mongo, body):
    assert client.post(URL, json=body).status_code == 200


@pytest.mark.parametrize(
    ("body", "field"),
    [
        (None, "body"),
        ([], "body"),
        ("text", "body"),
        ({"query": None}, "query"),
        ({"query": []}, "query"),
        ({"page": 0}, "page"),
        ({"page": -1}, "page"),
        ({"page": "1"}, "page"),
        ({"page": True}, "page"),
        ({"page": 1.0}, "page"),
        ({"page": MAX_PAGE + 1}, "page"),
        ({"page": 10**20}, "page"),  # its $skip would not fit a 64-bit integer
        ({"per_page": 0}, "per_page"),
        ({"per_page": -5}, "per_page"),
        ({"per_page": 101}, "per_page"),
        ({"per_page": "25"}, "per_page"),
        ({"per_page": 2.5}, "per_page"),
        ({"fetch_all": "true"}, "fetch_all"),
        ({"query": {"min_width": None}}, "min_width"),
        ({"query": {"min_width": -1}}, "min_width"),
        ({"query": {"min_width": 10**400}}, "min_width"),  # too large for a float
        ({"query": {"min_height": "0"}}, "min_height"),
        ({"query": {"label_quality_score": "50"}}, "label_quality_score"),
        ({"query": {"label_quality_score": 101}}, "label_quality_score"),
        ({"query": {"label_quality_score": -0.1}}, "label_quality_score"),
        ({"query": {"label_quality_score": 10**400}}, "label_quality_score"),
        ({"query": {"include_classes": "cat"}}, "include_classes"),
        ({"query": {"include_classes": [1]}}, "include_classes"),
        ({"query": {"include_classes": ["notaclass"]}}, "include_classes"),
        ({"query": {"exclude_classes": ["$where"]}}, "exclude_classes"),
        ({"query": {"exclude_classes": None}}, "exclude_classes"),
        ({"query": {"include_mode": "some"}}, "include_mode"),
        ({"query": {"include_mode": ["any"]}}, "include_mode"),
        ({"sort": "score"}, "sort"),
        ({"sort": {"_id": 1}}, "sort"),
        ({"sort": None}, "sort"),
    ],
)
def test_invalid_input_is_400(client, fake_mongo, body, field):
    response = client.post(URL, json=body)

    assert response.status_code == 400
    data = response.get_json()
    assert data["error"] == "Bad Request"
    assert field in data["fields"]
    assert field in data["message"]
    assert fake_mongo.pipelines == []


def test_invalid_input_lists_every_bad_field(client, fake_mongo):
    data = client.post(URL, json={"page": 0, "per_page": 500, "query": {"min_width": -1}}).get_json()
    assert set(data["fields"]) == {"page", "per_page", "min_width"}


@pytest.mark.parametrize("raw", ["not json", "NaN", '{"query": {"min_width": Infinity}}'])
def test_non_json_and_non_finite_numbers_are_400(client, fake_mongo, raw):
    response = client.post(URL, data=raw, content_type="application/json")
    assert response.status_code == 400
    assert fake_mongo.pipelines == []


@pytest.mark.parametrize("fetch_all", [False, True])
def test_mongo_failure_is_502(client, fake_mongo, monkeypatch, caplog, fetch_all):
    def fail(*args, **kwargs):
        raise OperationFailure("FieldPath field names may not start with '$' (secret detail)")

    monkeypatch.setattr(fake_mongo, "aggregate", fail)

    response = client.post(URL, json={"fetch_all": fetch_all})

    assert response.status_code == 502
    assert response.is_json
    assert "secret detail" not in response.get_data(as_text=True)
    assert "MongoDB query failed" in caplog.text


@pytest.mark.parametrize("fetch_all", [False, True])
def test_mongo_client_failure_is_502(client, monkeypatch, caplog, fetch_all):
    def unreachable():
        # What MongoClient raises when a mongodb+srv:// host does not resolve.
        raise ConfigurationError("The DNS query name does not exist: _mongodb._tcp.cluster0.example.invalid.")

    monkeypatch.setattr("app.routes.cortex.get_mongo_collection", unreachable)

    response = client.post(URL, json={"fetch_all": fetch_all})

    assert response.status_code == 502
    assert response.get_json() == {"error": "Bad Gateway", "message": "The image database is unavailable."}
    assert "MongoDB query failed" in caplog.text


# --- CSV export ----------------------------------------------------------------------------------


def test_csv_export_contains_every_matching_row(client, fake_mongo):
    response = client.post(URL, json={"fetch_all": True, "query": {}})

    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert response.headers["Content-Disposition"] == "attachment; filename=data.csv"
    assert response.is_streamed
    rows = csv_rows(response)
    assert rows[0] == CSV_HEADER
    assert len(rows) - 1 == MATCHING_DOCS
    ids = [row[0] for row in rows[1:]]
    assert ids == sorted(ids)
    assert all(row_id.startswith("PT::") for row_id in ids)
    # One query only, and it is not paginated.
    assert fake_mongo.pipelines == [[{"$match": DEFAULT_MATCH}, {"$sort": {"_id": 1}}, {"$limit": 100_000}]]


def test_csv_export_ignores_pagination(client, fake_mongo):
    rows = csv_rows(client.post(URL, json={"fetch_all": True, "page": 2, "per_page": 5}))
    assert len(rows) - 1 == MATCHING_DOCS


def test_csv_export_applies_the_filters(client, fake_mongo):
    rows = csv_rows(client.post(URL, json={"fetch_all": True, "query": {"include_classes": ["dog"]}}))
    assert len(rows) - 1 == 10


def test_csv_cells(client, fake_mongo):
    doc = fake_mongo.collection.find_one({"url": "https://images.example.com/0.jpg"})
    fake_mongo.collection.update_one({"_id": doc["_id"]}, {"$set": {"hash": "ab" * 32}})

    rows = csv_rows(client.post(URL, json={"fetch_all": True}))

    first = dict(zip(rows[0], rows[1]))
    assert first["_id"] == f"PT::{doc['_id']}"
    assert first["url"] == "https://images.example.com/0.jpg"
    assert (first["width"], first["height"]) == ("640", "480")
    assert first["hash"] == "ab" * 32
    assert json.loads(first["object_detection"]) == doc["object_detection"]
    assert float(first["label_quality_score"]) == doc["label_quality_score"]
    assert rows[2][CSV_HEADER.index("hash")] == ""  # rows stored before hashes existed stay empty


@pytest.mark.parametrize("url", ['=HYPERLINK("http://x")', "+1+1", "-2+3", "@SUM(A1)", "\t=1", "\r=1"])
def test_csv_neutralises_formulas(client, fake_mongo, url):
    fake_mongo.collection.update_many({}, {"$set": {"url": url}})

    rows = csv_rows(client.post(URL, json={"fetch_all": True}))

    assert {row[1] for row in rows[1:]} == {"'" + url}


def test_csv_safe_leaves_other_values_alone():
    assert _csv_safe("https://images.example.com/a.jpg") == "https://images.example.com/a.jpg"
    assert _csv_safe('{"dog": []}') == '{"dog": []}'
    assert _csv_safe(-5) == -5
    assert _csv_safe(None) is None
    assert _csv_safe("") == ""


def test_csv_export_is_capped(app, client, fake_mongo):
    app.config["EXPORT_MAX_ROWS"] = 7

    rows = csv_rows(client.post(URL, json={"fetch_all": True}))

    assert len(rows) - 1 == 7
    assert fake_mongo.pipelines[0][-1] == {"$limit": 7}
    everything = csv_rows(client.post(URL, json={"fetch_all": True, "per_page": 100}))
    assert rows[1:] == everything[1:8]  # the first rows in _id order


def test_csv_export_requires_a_subscription(logged_in_client, fake_mongo):
    assert logged_in_client.post(URL, json={"fetch_all": True}).status_code == 403
    assert fake_mongo.pipelines == []


def test_cursor_failure_during_export_is_logged_and_aborts(client, fake_mongo, monkeypatch, caplog):
    class FailingCursor:
        closed = False

        def __iter__(self):
            yield {"_id": 1, "url": "https://images.example.com/1.jpg"}
            raise PyMongoError("connection lost")

        def close(self):
            FailingCursor.closed = True

    monkeypatch.setattr(fake_mongo, "aggregate", lambda *args, **kwargs: FailingCursor())

    response = client.post(URL, json={"fetch_all": True})
    with pytest.raises(PyMongoError):
        response.get_data()

    assert FailingCursor.closed
    assert "MongoDB cursor failed during the CSV export" in caplog.text
