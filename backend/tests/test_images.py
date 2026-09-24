"""Per-image lookups and analysis requests (the browser extension's API)."""
from datetime import datetime, timezone

import mongomock
import pytest
from flask_login import FlaskLoginClient
from pymongo.errors import DuplicateKeyError, ExecutionTimeout, ServerSelectionTimeoutError

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import User
from app.routes.images import MAX_URLS, normalise_url

LOOKUP = "/api/images/lookup"
ANALYSE = "/api/images/analyse"
BOXES = {"dog": [[0.1, 0.2, 0.5, 0.6]]}


@pytest.fixture
def images(monkeypatch):
    """The routes' image collection: mongomock with the unique url index, seeded with every state."""
    collection = mongomock.MongoClient().cortex.collection
    collection.create_index("url", unique=True)
    collection.insert_many([
        {"_id": 1, "url": "https://img.example/queued.jpg", "datetime": "2026-09-01 10:00:00"},
        {"_id": 2, "url": "https://img.example/scoring.jpg", "object_detection": BOXES, "width": 400, "height": 200},
        {"_id": 3, "url": "https://img.example/done.jpg", "object_detection": BOXES, "width": 400, "height": 200,
         "label_quality_score": 0.9, "label_quality_model": "27040e830f3f"},
        {"_id": 4, "url": "https://img.example/empty.jpg", "object_detection": {}, "width": 10, "height": 10,
         "label_quality_score": None, "label_quality_error": "no detections"},
        {"_id": 5, "url": "https://img.example/broken.jpg", "object_detection": "error: broken image"},
    ])
    monkeypatch.setattr("app.routes.images.get_mongo_collection", lambda: collection)
    return collection


@pytest.fixture
def client(login_as, subscriber):
    return login_as(subscriber)


def test_normalise_url():
    assert normalise_url("HTTPS://Img.Example:443/a b.jpg") == "https://img.example/a%20b.jpg"
    for bad in [None, "", "   ", 5, "data:image/png;base64,AAAA", "ftp://img.example/a.jpg", "javascript:alert(1)",
                "https://" + "a" * 2050, "http:///nohost.jpg", "img.example/a.jpg", "//img.example/a.jpg",
                "http://[::1", "https://exa mple.com/a.jpg"]:
        assert normalise_url(bad) is None, bad


@pytest.mark.parametrize("path", [LOOKUP, ANALYSE])
def test_anonymous_is_401_and_no_access_is_403(app, login_as, user, images, path):
    body = {"urls": ["https://img.example/done.jpg"], "url": "https://img.example/done.jpg"}
    assert app.test_client().post(path, json=body).status_code == 401
    assert login_as(user).post(path, json=body).status_code == 403


def test_lookup_describes_every_state(client, images):
    urls = [
        "https://img.example/queued.jpg",
        "https://img.example/scoring.jpg",
        "HTTPS://IMG.EXAMPLE/done.jpg",  # matched in its normalised form
        "https://img.example/empty.jpg",
        "https://img.example/broken.jpg",
        "https://img.example/new.jpg",
        "data:image/png;base64,AAAA",
    ]

    response = client.post(LOOKUP, json={"urls": urls})

    assert response.status_code == 200
    results = response.get_json()["results"]
    assert set(results) == set(urls)
    assert results[urls[0]] == {"id": "PT::1", "requested": False, "status": "queued"}
    assert results[urls[1]] == {
        "id": "PT::2", "requested": False, "status": "scoring", "object_detection": BOXES, "width": 400, "height": 200,
    }
    assert results[urls[2]] == {
        "id": "PT::3", "requested": False, "status": "done", "object_detection": BOXES, "width": 400, "height": 200,
        "label_quality_score": 0.9,
    }
    assert results[urls[3]]["status"] == "done"
    assert results[urls[3]]["label_quality_score"] is None
    assert results[urls[3]]["label_quality_error"] == "no detections"
    assert results[urls[4]] == {"id": "PT::5", "requested": False, "status": "failed", "reason": "broken image"}
    assert results[urls[5]] == {"status": "unknown"}
    assert results[urls[6]] == {"status": "invalid"}


@pytest.mark.parametrize(
    "body",
    [None, [], {}, {"urls": "https://img.example/a.jpg"}, {"urls": []}, {"urls": [1]},
     {"urls": ["https://img.example/a.jpg"] * (MAX_URLS + 1)}],
)
def test_lookup_rejects_bad_bodies(client, images, body):
    response = client.post(LOOKUP, json=body)
    assert response.status_code == 400
    assert "urls" in response.get_json()["fields"]


def test_lookup_of_only_invalid_urls_needs_no_query(client, images, monkeypatch):
    monkeypatch.setattr(images, "find", lambda *a, **k: pytest.fail("queried MongoDB"), raising=False)
    response = client.post(LOOKUP, json={"urls": ["data:x", "blob:y"]})
    assert response.get_json()["results"] == {"data:x": {"status": "invalid"}, "blob:y": {"status": "invalid"}}


def test_analyse_queues_a_new_url_as_requested(client, images):
    response = client.post(ANALYSE, json={"url": "HTTPS://Img.Example/new.jpg"})

    assert response.status_code == 202
    data = response.get_json()
    assert data["url"] == "https://img.example/new.jpg"
    assert (data["status"], data["requested"]) == ("queued", True)
    document = images.find_one({"url": "https://img.example/new.jpg"})
    assert document["source"] == "extension"
    assert isinstance(document["datetime"], str)
    assert isinstance(document["requested_at"], datetime)
    assert "object_detection" not in document  # in the detection stage's queue


def test_analyse_twice_keeps_the_first_request_time(client, images):
    client.post(ANALYSE, json={"url": "https://img.example/new.jpg"})
    first = images.find_one({"url": "https://img.example/new.jpg"})["requested_at"]

    assert client.post(ANALYSE, json={"url": "https://img.example/new.jpg"}).status_code == 202

    assert images.count_documents({"url": "https://img.example/new.jpg"}) == 1
    assert images.find_one({"url": "https://img.example/new.jpg"})["requested_at"] == first


@pytest.mark.parametrize(("url", "status"), [("queued", "queued"), ("scoring", "scoring")])
def test_analyse_moves_a_waiting_image_ahead(client, images, url, status):
    response = client.post(ANALYSE, json={"url": f"https://img.example/{url}.jpg"})

    assert response.status_code == 202
    assert (response.get_json()["status"], response.get_json()["requested"]) == (status, True)
    document = images.find_one({"url": f"https://img.example/{url}.jpg"})
    assert "requested_at" in document
    assert "source" not in document  # the crawl stored it


@pytest.mark.parametrize(("url", "status"), [("done", "done"), ("empty", "done"), ("broken", "failed")])
def test_analyse_of_a_processed_image_answers_its_result(client, images, url, status):
    response = client.post(ANALYSE, json={"url": f"https://img.example/{url}.jpg"})

    assert response.status_code == 200
    assert response.get_json()["status"] == status
    assert "requested_at" not in images.find_one({"url": f"https://img.example/{url}.jpg"})


def test_analyse_survives_a_concurrent_insert(client, images, monkeypatch):
    real_update = images.update_one
    calls = []

    def racing_update(query, update, upsert=False):
        calls.append(upsert)
        if upsert:  # another request inserts the same URL just before this upsert
            images.insert_one({"url": query["url"]})
            raise DuplicateKeyError("E11000 duplicate key error")
        return real_update(query, update)

    monkeypatch.setattr(images, "update_one", racing_update, raising=False)
    response = client.post(ANALYSE, json={"url": "https://img.example/race.jpg"})

    assert response.status_code == 202
    assert calls == [True, False]
    assert images.count_documents({"url": "https://img.example/race.jpg"}) == 1
    assert "requested_at" in images.find_one({"url": "https://img.example/race.jpg"})


@pytest.mark.parametrize("body", [None, {}, {"url": 5}, {"url": "data:image/png;base64,AA"}, {"url": "ftp://x/y"}])
def test_analyse_rejects_bad_urls(client, images, body):
    response = client.post(ANALYSE, json=body)
    assert response.status_code == 400
    assert "url" in response.get_json()["fields"]


@pytest.mark.parametrize(("path", "body"), [(LOOKUP, {"urls": ["https://img.example/a.jpg"]}),
                                            (ANALYSE, {"url": "https://img.example/a.jpg"})])
def test_mongo_failure_is_502(client, monkeypatch, caplog, path, body):
    def unavailable():
        raise ServerSelectionTimeoutError("no servers")

    monkeypatch.setattr("app.routes.images.get_mongo_collection", unavailable)

    response = client.post(path, json=body)

    assert response.status_code == 502
    assert "MongoDB query failed" in caplog.text


def test_lookup_timeout_is_504(client, images, monkeypatch):
    def slow(*args, **kwargs):
        raise ExecutionTimeout("operation exceeded time limit")

    monkeypatch.setattr(images, "find", slow, raising=False)
    response = client.post(LOOKUP, json={"urls": ["https://img.example/done.jpg"]})

    assert response.status_code == 504


# --- rate limits ---------------------------------------------------------------------------------


class RateLimitedConfig(TestConfig):
    RATELIMIT_ENABLED = True


@pytest.fixture
def limited_app():
    app = create_app(RateLimitedConfig)
    app.test_client_class = FlaskLoginClient
    with app.app_context():
        db.create_all()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def make_subscriber(app, email):
    with app.app_context():
        user = User(email=email, name="S", subscription_status="active")
        user.set_password("secret123")
        db.session.add(user)
        db.session.commit()
        db.session.refresh(user)
        db.session.expunge(user)
        return user


def test_analyse_is_limited_per_user(limited_app, images):
    first = limited_app.test_client(user=make_subscriber(limited_app, "a@example.com"))
    second = limited_app.test_client(user=make_subscriber(limited_app, "b@example.com"))

    statuses = [first.post(ANALYSE, json={"url": f"https://img.example/{i}.jpg"}).status_code for i in range(11)]

    assert 429 not in statuses[:10]
    assert statuses[10] == 429
    assert first.post(ANALYSE, json={"url": "https://img.example/x.jpg"}).get_json()["error"] == "Too Many Requests"
    # The limit is per user, not per address: another user from the same address gets through.
    assert second.post(ANALYSE, json={"url": "https://img.example/y.jpg"}).status_code == 202


def test_anonymous_requests_are_limited_per_address(limited_app, images):
    client = limited_app.test_client()
    statuses = [
        client.post(ANALYSE, json={"url": "https://img.example/a.jpg"}, environ_base={"REMOTE_ADDR": "198.51.100.7"})
        .status_code
        for _ in range(11)
    ]
    assert statuses[:10] == [401] * 10
    assert statuses[10] == 429


def test_requested_time_is_utc(client, images):
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    client.post(ANALYSE, json={"url": "https://img.example/new.jpg"})
    stored = images.find_one({"url": "https://img.example/new.jpg"})["requested_at"]
    assert stored.replace(tzinfo=None) >= before.replace(microsecond=0)
