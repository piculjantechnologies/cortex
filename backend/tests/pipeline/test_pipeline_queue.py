"""Requested documents (the web app's analyse endpoint) come first in every stage's round."""
from datetime import datetime, timedelta

import pytest

mongomock = pytest.importorskip("mongomock")
pytest.importorskip("pymongo")

from pipeline.db import draw, ensure_indexes, has_requests  # noqa: E402

pytestmark = pytest.mark.pipeline

QUEUE = {"object_detection": {"$exists": False}}
T0 = datetime(2026, 9, 24, 12, 0, 0)


@pytest.fixture
def collection():
    coll = mongomock.MongoClient().cortex.collection
    coll.insert_many([{"_id": i, "url": f"https://img.example/{i}.jpg"} for i in range(10)])
    return coll


def test_requested_documents_come_first_oldest_request_first(collection):
    collection.update_one({"_id": 7}, {"$set": {"requested_at": T0 + timedelta(seconds=5)}})
    collection.update_one({"_id": 3}, {"$set": {"requested_at": T0}})

    documents = draw(collection, QUEUE, 5)

    assert [d["_id"] for d in documents[:2]] == [3, 7]
    assert len(documents) == 5
    assert len({d["_id"] for d in documents}) == 5


def test_a_round_of_only_requests(collection):
    for i in range(10):
        collection.update_one({"_id": i}, {"$set": {"requested_at": T0 + timedelta(seconds=10 - i)}})
    assert [d["_id"] for d in draw(collection, QUEUE, 3)] == [9, 8, 7]


def test_processed_documents_are_never_drawn_even_when_requested(collection):
    collection.update_one({"_id": 1}, {"$set": {"requested_at": T0, "object_detection": {}}})
    assert 1 not in {d["_id"] for d in draw(collection, QUEUE, 20)}
    assert not has_requests(collection, QUEUE)


def test_has_requests(collection):
    assert not has_requests(collection, QUEUE)
    collection.update_one({"_id": 2}, {"$set": {"requested_at": T0}})
    assert has_requests(collection, QUEUE)


def test_requested_at_index_is_sparse():
    coll = mongomock.MongoClient().cortex.collection
    ensure_indexes(coll)
    assert coll.index_information()["requested_at_1"].get("sparse") is True
