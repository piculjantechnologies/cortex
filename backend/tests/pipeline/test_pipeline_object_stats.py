"""Per-class box statistics and their backfill."""
import pytest

mongomock = pytest.importorskip("mongomock")
pytest.importorskip("pymongo")

from pipeline import backfill_object_stats as backfill  # noqa: E402
from pipeline.object_stats import box_area, object_stats  # noqa: E402

pytestmark = pytest.mark.pipeline


def test_box_area_is_clipped_to_the_image():
    assert box_area([0.25, 0.25, 0.75, 0.75]) == 0.25
    assert box_area([-0.5, 0.0, 0.5, 1.0]) == 0.5
    assert box_area([0.5, 0.5, 0.4, 0.9]) == 0.0  # degenerate box


def test_object_stats_counts_and_largest_area_per_class():
    stats = object_stats({
        "person": [[0, 0, 0.5, 0.5], [0, 0, 0.1, 0.1], [0.5, 0.5, 1, 1]],
        "dog": [[0, 0, 1, 1]],
        "cat": [],
    })
    assert stats == {
        "object_counts": {"person": 3, "dog": 1},
        "object_max_area": {"person": 0.25, "dog": 1.0},
        "object_total": 4,
    }


def test_object_stats_of_an_image_without_boxes():
    assert object_stats({}) == {"object_counts": {}, "object_max_area": {}, "object_total": 0}


@pytest.fixture
def collection():
    coll = mongomock.MongoClient().cortex.collection
    coll.insert_many([
        {"_id": 1, "object_detection": {"dog": [[0, 0, 0.5, 1]]}},
        {"_id": 2, "object_detection": {}},
        {"_id": 3, "object_detection": "error: broken image"},
        {"_id": 4, "url": "https://img.example/queued.png"},
        {"_id": 5, "object_detection": {"cat": [[0, 0, 1, 1]]},
         "object_counts": {"cat": 7}, "object_max_area": {"cat": 1.0}, "object_total": 7},
    ])
    return coll


def test_backfill_sets_the_fields_on_detected_documents_without_them(collection):
    assert backfill.run(collection, batch=1) == 2

    assert collection.find_one({"_id": 1})["object_counts"] == {"dog": 1}
    assert collection.find_one({"_id": 1})["object_max_area"] == {"dog": 0.5}
    assert collection.find_one({"_id": 2})["object_total"] == 0
    assert "object_total" not in collection.find_one({"_id": 3})
    assert "object_total" not in collection.find_one({"_id": 4})
    assert collection.find_one({"_id": 5})["object_total"] == 7  # already had the fields
    assert backfill.run(collection) == 0  # nothing left


def test_backfill_all_recomputes_every_detected_document(collection):
    assert backfill.run(collection, everything=True) == 3
    assert collection.find_one({"_id": 5})["object_counts"] == {"cat": 1}


def test_backfill_parse_args():
    assert (backfill.parse_args([]).all, backfill.parse_args([]).batch) == (False, 1000)
    assert backfill.parse_args(["--all", "--batch", "10"]).batch == 10
