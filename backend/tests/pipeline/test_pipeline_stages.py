"""Detection and scoring stages with stub models, stub fetchers and mongomock."""
import hashlib
import io
import struct
import zlib
from datetime import datetime

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("torchvision")
np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")
Image = pytest.importorskip("PIL.Image")  # installed with torchvision
mongomock = pytest.importorskip("mongomock")

from torchvision.models.detection import SSDLite320_MobileNet_V3_Large_Weights  # noqa: E402

from pipeline import (  # noqa: E402
    detect_objects as detect,
    estimate_label_quality as estimate,
    labelqa,
)
from pipeline.http import FetchError, RetryLater  # noqa: E402
from pipeline.labelqa import thesis  # noqa: E402
from pipeline.labelqa.thesis.preprocessing import MAPPING  # noqa: E402

pytestmark = pytest.mark.pipeline

CPU = torch.device("cpu")
CATEGORIES = SSDLite320_MobileNet_V3_Large_Weights.DEFAULT.meta["categories"]


def encode(image, fmt, **kwargs):
    buf = io.BytesIO()
    image.save(buf, fmt, **kwargs)
    return buf.getvalue()


def exif_rotated_jpeg():
    """200 x 100 pixels stored, EXIF Orientation=6 (displayed rotated to 100 x 200)."""
    exif = Image.Exif()
    exif[0x0112] = 6
    return encode(Image.new("RGB", (200, 100), (200, 30, 30)), "JPEG", exif=exif.tobytes())


def png(width=64, height=48, color=(10, 200, 10)):
    return encode(Image.new("RGB", (width, height), color), "PNG")


def animated_gif(frames=3):
    images = [Image.new("RGB", (40, 30), (i * 80, 0, 0)) for i in range(frames)]
    return encode(images[0], "GIF", save_all=True, append_images=images[1:], duration=100)


def png_header_only(width, height):
    """A PNG signature, IHDR and IEND: the header declares the size, there are no pixels."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


@pytest.fixture
def collection():
    return mongomock.MongoClient().cortex.collection


# decoding

def test_exif_orientation_gives_the_same_size_in_both_stages():
    data = exif_rotated_jpeg()
    tensor = detect.decode(data)
    array = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    assert tuple(tensor.shape) == (3, 200, 100)
    assert array.shape[:2] == tuple(tensor.shape[1:])


@pytest.mark.parametrize("image,fmt", [
    (Image.new("L", (30, 20), 128), "JPEG"),
    (Image.new("P", (30, 20), 3), "PNG"),
    (Image.new("LA", (30, 20), (128, 255)), "PNG"),
    (Image.new("RGBA", (30, 20), (1, 2, 3, 4)), "PNG"),
    (Image.new("CMYK", (30, 20), (0, 0, 0, 0)), "JPEG"),
], ids=["gray-jpeg", "palette-png", "la-png", "rgba-png", "cmyk-jpeg"])
def test_non_rgb_images_decode_to_three_channels(image, fmt):
    tensor = detect.decode(encode(image, fmt))
    assert tensor.dtype == torch.uint8
    assert tuple(tensor.shape) == (3, 20, 30)


def test_cmyk_black_pixel_stays_dark():
    image = Image.new("CMYK", (16, 16), (0, 0, 0, 0))  # white
    image.paste((0, 0, 0, 255), (0, 0, 8, 16))  # K only: black
    tensor = detect.decode(encode(image, "JPEG", quality=100))
    assert tensor[:, :, :4].max() < 60
    assert tensor[:, :, 12:].min() > 195


@pytest.mark.parametrize("data", [b"", b"not an image", b"\x89PNG\r\n\x1a\n" + b"\x00" * 20])
def test_undecodable_bytes_raise(data):
    with pytest.raises((RuntimeError, ValueError)):
        detect.decode(data)


def test_animated_gif_is_not_a_single_frame_image():
    with pytest.raises(ValueError, match="single-frame") as excinfo:
        detect.decode(animated_gif())
    assert not isinstance(excinfo.value, detect.ImageTooLarge)


@pytest.mark.parametrize("width,height", [(detect.MAX_SIDE + 1, 1), (1, 20000), (100_000, 100_000)])
def test_oversized_header_is_refused_before_decoding(monkeypatch, width, height):
    monkeypatch.setattr(detect, "decode_image", lambda *a, **k: pytest.fail("decoded an oversized image"))
    with pytest.raises(detect.ImageTooLarge):
        detect.decode(png_header_only(width, height))


def test_largest_allowed_side_passes_the_header_check():
    detect.check_size(png_header_only(detect.MAX_SIDE, detect.MAX_SIDE))


# detection

class StubDetector:
    """Returns the given predictions (one dict per image) and records the batch sizes."""

    def __init__(self, predictions):
        self.predictions = predictions
        self.batches = []

    def __call__(self, batch):
        self.batches.append(len(batch))
        return [self.predictions[i % len(self.predictions)] for i in range(len(batch))]


def prediction(detections):
    """detections: [(score, coco_name, [x1, y1, x2, y2])]"""
    return {
        "scores": torch.tensor([d[0] for d in detections], dtype=torch.float32),
        "labels": torch.tensor([CATEGORIES.index(d[1]) for d in detections], dtype=torch.int64),
        "boxes": torch.tensor([d[2] for d in detections], dtype=torch.float32).reshape(-1, 4),
    }


def half_size(image):
    """A preprocess that halves the resolution, so boxes must be scaled back by 2."""
    return image[:, ::2, ::2].float() / 255


def test_detect_objects_filters_rescales_and_maps_to_voc():
    image = torch.zeros(3, 100, 200, dtype=torch.uint8)
    model = StubDetector([prediction([
        (0.95, "person", [10, 5, 50, 25]),
        (0.50, "dog", [0, 0, 10, 10]),
        (0.99, "motorcycle", [0, 0, 100, 50]),
        (0.92, "traffic light", [1, 1, 2, 2]),
        (0.91, "tv", [50, 25, 100, 50]),
    ])])
    [(scores, boxes, labels, width, height)] = detect.detect_objects([image], model, half_size, CPU)
    assert (width, height) == (200, 100)
    assert scores.tolist() == pytest.approx([0.95, 0.99, 0.92, 0.91])
    assert boxes[0].tolist() == [20, 10, 100, 50]  # scaled back to the original size

    object_detection = detect.build_object_detection(boxes, labels, width, height, CATEGORIES)
    expected = {
        "person": [[0.1, 0.1, 0.5, 0.5]],
        "motorbike": [[0.0, 0.0, 1.0, 1.0]],
        "tvmonitor": [[0.5, 0.5, 1.0, 1.0]],
    }
    assert object_detection.keys() == expected.keys()
    for name, boxes_ in expected.items():
        assert object_detection[name] == [pytest.approx(box) for box in boxes_]
        assert all(type(v) is float for box in object_detection[name] for v in box)


def test_detect_objects_runs_one_model_call_per_batch():
    images = [torch.zeros(3, 10 + i, 20, dtype=torch.uint8) for i in range(3)]
    model = StubDetector([prediction([])])
    results = detect.detect_objects(images, model, half_size, CPU)
    assert model.batches == [3]
    assert [(r[3], r[4]) for r in results] == [(20, 10), (20, 11), (20, 12)]


def run_detect(collection, fetcher, model, **kwargs):
    return detect.run(collection, model, half_size, CATEGORIES, CPU, fetcher, **kwargs)


def test_detect_stage_stores_results_hash_and_error_markers(collection, stub_fetcher):
    good = png(64, 48)
    huge = encode(Image.new("L", (detect.MAX_SIDE + 1, 1)), "PNG")
    collection.insert_many([
        {"_id": 1, "url": "https://img.example/good.png"},
        {"_id": 2, "url": "https://img.example/missing.png"},
        {"_id": 3, "url": "https://img.example/garbage.png"},
        {"_id": 4, "url": "https://img.example/huge.png"},
        {"_id": 5, "url": "https://img.example/done.png", "object_detection": {"cat": []}},
    ])
    fetcher = stub_fetcher({
        "https://img.example/good.png": good,
        "https://img.example/garbage.png": b"<html>",
        "https://img.example/huge.png": huge,
    })
    model = StubDetector([prediction([(0.97, "cat", [8, 6, 32, 24])])])
    assert run_detect(collection, fetcher, model, batch=2) == 4

    doc = collection.find_one({"_id": 1})
    assert doc["object_detection"] == {"cat": [[0.25, 0.25, 1.0, 1.0]]}
    assert (doc["object_counts"], doc["object_max_area"], doc["object_total"]) == ({"cat": 1}, {"cat": 0.5625}, 1)
    assert (doc["width"], doc["height"]) == (64, 48)
    assert doc["hash"] == hashlib.sha256(good).hexdigest()
    assert collection.find_one({"_id": 2})["object_detection"] == detect.BROKEN_IMAGE
    assert collection.find_one({"_id": 3})["object_detection"] == detect.BROKEN_IMAGE
    assert collection.find_one({"_id": 4})["object_detection"] == detect.TOO_LARGE
    assert "hash" not in collection.find_one({"_id": 2})
    assert "object_total" not in collection.find_one({"_id": 2})
    assert collection.find_one({"_id": 5})["object_detection"] == {"cat": []}  # untouched
    assert "https://img.example/done.png" not in fetcher.calls
    assert sum(model.batches) == 1


class ShapeCheckingDetector(StubDetector):
    """Like the real detector, refuses anything but (3, H, W) images."""

    def __call__(self, batch):
        assert all(image.ndim == 3 and image.shape[0] == 3 for image in batch)
        return super().__call__(batch)


def test_detect_stage_marks_animated_image_broken_and_detects_the_rest(collection, stub_fetcher):
    collection.insert_many([
        {"_id": 1, "url": "https://img.example/good.png"},
        {"_id": 2, "url": "https://img.example/anim.gif"},
    ])
    fetcher = stub_fetcher({"https://img.example/good.png": png(), "https://img.example/anim.gif": animated_gif()})
    model = ShapeCheckingDetector([prediction([(0.97, "cat", [8, 6, 32, 24])])])
    assert run_detect(collection, fetcher, model, batch=8) == 2
    assert collection.find_one({"_id": 2})["object_detection"] == detect.BROKEN_IMAGE
    assert collection.find_one({"_id": 1})["object_detection"] == {"cat": [[0.25, 0.25, 1.0, 1.0]]}
    assert model.batches == [1]


def test_detect_stage_marks_oversized_header_too_large(collection, stub_fetcher, monkeypatch):
    monkeypatch.setattr(detect, "decode_image", lambda *a, **k: pytest.fail("decoded an oversized image"))
    collection.insert_one({"_id": 1, "url": "https://img.example/bomb.png"})
    fetcher = stub_fetcher({"https://img.example/bomb.png": png_header_only(100_000, 100_000)})
    assert run_detect(collection, fetcher, StubDetector([prediction([])])) == 1
    assert collection.find_one({"_id": 1})["object_detection"] == detect.TOO_LARGE


def test_detect_stage_leaves_waiting_documents_queued(collection, stub_fetcher):
    collection.insert_many([
        {"_id": 1, "url": "https://img.example/good.png"},
        {"_id": 2, "url": "https://busy.example/a.png"},
    ])
    fetcher = stub_fetcher({
        "https://img.example/good.png": png(),
        "https://busy.example/a.png": RetryLater("busy.example is paused; retry later"),
    })
    # The second round only draws the waiting document and ends the run.
    assert run_detect(collection, fetcher, StubDetector([prediction([])])) == 1
    assert "object_detection" not in collection.find_one({"_id": 2})
    assert fetcher.calls.count("https://busy.example/a.png") == 2


def test_detect_daemon_sleeps_while_documents_wait(collection, stub_fetcher, monkeypatch):
    class Stop(Exception):
        pass

    def fake_sleep(seconds):
        raise Stop

    monkeypatch.setattr(detect, "sleep", fake_sleep)
    collection.insert_one({"url": "https://busy.example/a.png"})
    fetcher = stub_fetcher({"https://busy.example/a.png": RetryLater("paused")})
    with pytest.raises(Stop):
        run_detect(collection, fetcher, StubDetector([prediction([])]), daemon=True)
    assert fetcher.calls == ["https://busy.example/a.png"]


def test_detect_stage_exits_on_empty_queue(collection, stub_fetcher, monkeypatch):
    monkeypatch.setattr(detect, "sleep", lambda s: pytest.fail("slept without --daemon"))
    assert run_detect(collection, stub_fetcher({}), StubDetector([prediction([])])) == 0


def test_detect_stage_honours_limit(collection, stub_fetcher):
    collection.insert_many([{"url": f"https://img.example/{i}.png"} for i in range(5)])
    fetcher = stub_fetcher({f"https://img.example/{i}.png": png() for i in range(5)})
    assert run_detect(collection, fetcher, StubDetector([prediction([])]), limit=2) == 2
    assert collection.count_documents({"object_detection": {"$exists": True}}) == 2


def test_detect_stage_takes_a_request_made_during_a_round_next(collection, stub_fetcher):
    collection.insert_many([{"_id": i, "url": f"https://img.example/{i}.png"} for i in range(4)])
    fetcher = stub_fetcher({f"https://img.example/{i}.png": png() for i in range(4)})
    fetcher.routes["https://img.example/asked.png"] = png()
    real_fetch = fetcher.fetch

    def fetch(url, *args, **kwargs):
        if len(fetcher.calls) == 0:  # a user asks for an image while the first batch runs
            collection.insert_one({"_id": "asked", "url": "https://img.example/asked.png",
                                   "requested_at": datetime(2026, 9, 24)})
        return real_fetch(url, *args, **kwargs)

    fetcher.fetch = fetch
    assert run_detect(collection, fetcher, StubDetector([prediction([])]), batch=1) == 5

    assert fetcher.calls[1] == "https://img.example/asked.png"


def test_estimate_stage_scores_requested_documents_first(collection, stub_fetcher):
    collection.insert_many([
        {"_id": i, "url": f"https://img.example/{i}.png", "object_detection": {"cat": [[0, 0, 1, 1]]}}
        for i in range(5)
    ])
    collection.update_one({"_id": 4}, {"$set": {"requested_at": datetime(2026, 9, 24)}})
    fetcher = stub_fetcher({f"https://img.example/{i}.png": png() for i in range(5)})

    run_estimate(collection, fetcher, batch=1, limit=1)

    assert fetcher.calls == ["https://img.example/4.png"]


def test_detect_daemon_sleeps_on_empty_queue(collection, stub_fetcher, monkeypatch):
    class Stop(Exception):
        pass

    slept = []

    def fake_sleep(seconds):
        slept.append(seconds)
        raise Stop

    monkeypatch.setattr(detect, "sleep", fake_sleep)
    with pytest.raises(Stop):
        run_detect(collection, stub_fetcher({}), StubDetector([prediction([])]), daemon=True)
    assert slept == [detect.IDLE_SLEEP]


# scoring

MODEL_ID = "0123456789ab"


class StubNetwork:
    """Stands in for the thesis CorrNet: returns fixed logits for every sample, records batch sizes and planes."""

    def __init__(self, logits=(0.0, 2.0)):
        self.logits = torch.tensor(logits)
        self.batches = []
        self.labels = []

    def __call__(self, images, labels):
        assert images.shape[1:] == (3, 224, 224)
        assert labels.shape[1:] == (20, 224, 224)
        self.batches.append(images.shape[0])
        self.labels.extend(labels)
        return self.logits.repeat(images.shape[0], 1)


def scorer(network=None):
    """A Scorer with the thesis model's preprocessing and a StubNetwork."""
    return labelqa.Scorer(labelqa.MODELS["thesis"], network or StubNetwork(), CPU)


def gray(width=120, height=80):
    return np.full((height, width, 3), 127, dtype=np.uint8)


def test_images_are_decoded_to_bgr(stub_fetcher):
    fetcher = stub_fetcher({"https://img.example/red.png": png(4, 3, (200, 30, 10))})
    image = estimate.get_image_from_url("https://img.example/red.png", fetcher)
    assert image.shape == (3, 4, 3) and image.dtype == np.uint8
    assert image[0, 0].tolist() == [10, 30, 200]


def test_get_label_quality_score_with_stub_model_is_a_probability():
    score = estimate.get_label_quality_score(gray(), {"cat": [[0.1, 0.1, 0.5, 0.5]]}, scorer())
    assert isinstance(score, float)
    assert 0.0 <= score <= 1.0
    assert score == pytest.approx(torch.softmax(torch.tensor([0.0, 2.0]), 0)[1].item())


def test_prepare_places_boxes_in_the_letterboxed_frame():
    image, labels = thesis.prepare(gray(200, 100), {"person": [[0.0, 0.0, 1.0, 1.0]]})
    assert image.shape == (3, 224, 224)
    assert labels.shape == (20, 224, 224)
    person = MAPPING["person"]
    assert labels[person].max() == 1.0
    assert labels.sum() == labels[person].sum()
    # 200 x 100 resizes to 224 x 112, centred vertically: rows 0-55 stay empty
    assert labels[:, :56].max() == 0.0
    assert labels[person, 56, 0] == 1.0 and labels[person, 56 + 111, 223] == 1.0


def run_estimate(collection, fetcher, network=None, **kwargs):
    return estimate.run(collection, scorer(network), fetcher, MODEL_ID, **kwargs)


def test_estimate_stage_scores_and_marks_failures(collection, stub_fetcher):
    image = png(120, 80)
    collection.insert_many([
        {"_id": 1, "url": "https://img.example/ok.png", "object_detection": {"cat": [[0.1, 0.1, 0.9, 0.9]]}},
        {"_id": 2, "url": "https://img.example/gone.png", "object_detection": {"dog": [[0.1, 0.1, 0.9, 0.9]]}},
        {"_id": 3, "url": "https://img.example/empty.png", "object_detection": {}},
        {"_id": 4, "url": "https://img.example/broken.png", "object_detection": "error: broken image"},
        {"_id": 5, "url": "https://img.example/big.png", "object_detection": "error: too large"},
        {"_id": 6, "url": "https://img.example/noimg.png", "object_detection": {"cat": [[0, 0, 1, 1]]}},
        {"_id": 7, "url": "https://img.example/queued.png"},
        {"_id": 8, "url": "https://img.example/scored.png", "object_detection": {"cat": []},
         "label_quality_score": 0.7},
        {"_id": 9, "url": "https://img.example/other.png", "object_detection": {"giraffe": [[0, 0, 1, 1]]}},
    ])
    fetcher = stub_fetcher({
        "https://img.example/ok.png": image,
        "https://img.example/gone.png": FetchError("HTTP 404"),
        "https://img.example/noimg.png": b"<html>",
    })
    model = StubNetwork()
    assert run_estimate(collection, fetcher, model) == 5

    ok = collection.find_one({"_id": 1})
    assert 0.0 <= ok["label_quality_score"] <= 1.0
    assert ok["label_quality_model"] == MODEL_ID
    assert "label_quality_error" not in ok
    gone = collection.find_one({"_id": 2})
    assert gone["label_quality_score"] is None
    assert gone["label_quality_error"] == "fetch: HTTP 404"
    assert gone["label_quality_model"] == MODEL_ID
    for _id in (3, 9):  # no boxes of a Pascal VOC class
        empty = collection.find_one({"_id": _id})
        assert empty["label_quality_score"] is None
        assert empty["label_quality_error"] == "no detections"
        assert empty["label_quality_model"] == MODEL_ID
    assert collection.find_one({"_id": 6})["label_quality_score"] is None
    for _id in (4, 5, 7):
        assert "label_quality_score" not in collection.find_one({"_id": _id})
    assert collection.find_one({"_id": 8}) == {"_id": 8, "url": "https://img.example/scored.png",
                                               "object_detection": {"cat": []}, "label_quality_score": 0.7}
    assert "https://img.example/empty.png" not in fetcher.calls
    assert "https://img.example/other.png" not in fetcher.calls
    assert model.batches == [1]

    # everything left the queue: a second run finds nothing to do
    assert run_estimate(collection, fetcher) == 0


def test_estimate_stage_skips_classes_outside_pascal_voc(collection, stub_fetcher):
    collection.insert_one({"url": "https://img.example/a.png",
                           "object_detection": {"cat": [[0.1, 0.1, 0.5, 0.5]], "giraffe": [[0.5, 0.5, 0.9, 0.9]]}})
    model = StubNetwork()
    assert run_estimate(collection, stub_fetcher({"https://img.example/a.png": png(120, 80)}), model) == 1
    _, expected = thesis.prepare(gray(120, 80), {"cat": [[0.1, 0.1, 0.5, 0.5]]})
    assert np.array_equal(model.labels[0].numpy(), expected)
    assert collection.find_one()["label_quality_model"] == MODEL_ID


def test_estimate_stage_batches_model_calls(collection, stub_fetcher):
    collection.insert_many([{"url": f"https://img.example/{i}.png", "object_detection": {"cat": [[0, 0, 1, 1]]}}
                            for i in range(5)])
    fetcher = stub_fetcher({f"https://img.example/{i}.png": png() for i in range(5)})
    model = StubNetwork()
    assert run_estimate(collection, fetcher, model, batch=2) == 5
    assert model.batches == [2, 2, 1]
    scored = {"label_quality_score": {"$type": "double"}, "label_quality_model": MODEL_ID}
    assert collection.count_documents(scored) == 5


def rescore_documents(collection):
    boxes = {"cat": [[0.1, 0.1, 0.9, 0.9]]}
    collection.insert_many([
        {"_id": "unscored", "url": "https://img.example/1.png", "object_detection": boxes},
        {"_id": "old", "url": "https://img.example/2.png", "object_detection": boxes, "label_quality_score": 0.01},
        {"_id": "old-error", "url": "https://img.example/3.png", "object_detection": boxes,
         "label_quality_score": None, "label_quality_error": "fetch: HTTP 503"},
        {"_id": "other-model", "url": "https://img.example/4.png", "object_detection": boxes,
         "label_quality_score": 0.5, "label_quality_model": "ffffffffffff"},
        {"_id": "current", "url": "https://img.example/5.png", "object_detection": boxes,
         "label_quality_score": 0.3, "label_quality_model": MODEL_ID},
        {"_id": "current-error", "url": "https://img.example/6.png", "object_detection": boxes,
         "label_quality_score": None, "label_quality_error": "fetch: HTTP 404", "label_quality_model": MODEL_ID},
        {"_id": "broken", "url": "https://img.example/7.png", "object_detection": "error: broken image"},
        {"_id": "undetected", "url": "https://img.example/8.png"},
    ])
    return {f"https://img.example/{i}.png": png() for i in range(1, 9)}


def test_queue_selection_with_and_without_rescore(collection):
    rescore_documents(collection)

    def selected(query):
        return sorted(d["_id"] for d in collection.find(query))

    assert estimate.queue(MODEL_ID) is estimate.QUEUE
    assert selected(estimate.queue(MODEL_ID)) == ["unscored"]
    assert selected(estimate.queue(MODEL_ID, rescore=True)) == ["old", "old-error", "other-model", "unscored"]


def test_estimate_stage_without_rescore_scores_only_unscored_documents(collection, stub_fetcher):
    fetcher = stub_fetcher(rescore_documents(collection))
    assert run_estimate(collection, fetcher) == 1
    assert fetcher.calls == ["https://img.example/1.png"]
    assert collection.find_one({"_id": "old"}) == {
        "_id": "old", "url": "https://img.example/2.png", "object_detection": {"cat": [[0.1, 0.1, 0.9, 0.9]]},
        "label_quality_score": 0.01}


def test_estimate_stage_rescore(collection, stub_fetcher, monkeypatch):
    monkeypatch.setattr(estimate, "sleep", lambda s: pytest.fail("slept without --daemon"))
    routes = rescore_documents(collection)
    routes["https://img.example/4.png"] = FetchError("HTTP 410")
    fetcher = stub_fetcher(routes)
    p_good = torch.softmax(torch.tensor([0.0, 2.0]), 0)[1].item()

    assert run_estimate(collection, fetcher, rescore=True) == 4
    assert sorted(fetcher.calls) == [f"https://img.example/{i}.png" for i in (1, 2, 3, 4)]
    for _id in ("unscored", "old", "old-error"):
        document = collection.find_one({"_id": _id})
        assert document["label_quality_score"] == pytest.approx(p_good)
        assert document["label_quality_model"] == MODEL_ID
        assert "label_quality_error" not in document
    failed = collection.find_one({"_id": "other-model"})
    assert failed["label_quality_score"] is None
    assert failed["label_quality_error"] == "fetch: HTTP 410"
    assert failed["label_quality_model"] == MODEL_ID
    assert collection.find_one({"_id": "current"})["label_quality_score"] == 0.3
    assert collection.find_one({"_id": "current-error"})["label_quality_error"] == "fetch: HTTP 404"

    # every document now carries the current model: the queue is empty, failures included
    assert run_estimate(collection, fetcher, rescore=True) == 0


def test_estimate_stage_rescore_honours_limit(collection, stub_fetcher):
    fetcher = stub_fetcher(rescore_documents(collection))
    assert run_estimate(collection, fetcher, rescore=True, limit=2) == 2
    assert collection.count_documents({"label_quality_model": MODEL_ID}) == 2 + 2


def test_estimate_stage_leaves_waiting_documents_queued(collection, stub_fetcher, monkeypatch):
    monkeypatch.setattr(estimate, "sleep", lambda s: pytest.fail("slept without --daemon"))
    collection.insert_many([
        {"_id": 1, "url": "https://img.example/ok.png", "object_detection": {"cat": [[0, 0, 1, 1]]}},
        {"_id": 2, "url": "https://busy.example/a.png", "object_detection": {"cat": [[0, 0, 1, 1]]}},
    ])
    fetcher = stub_fetcher({
        "https://img.example/ok.png": png(),
        "https://busy.example/a.png": RetryLater("busy.example is paused; retry later"),
    })
    assert run_estimate(collection, fetcher) == 1
    waiting = collection.find_one({"_id": 2})
    assert "label_quality_score" not in waiting and "label_quality_error" not in waiting
    assert "label_quality_model" not in waiting
    assert fetcher.calls.count("https://busy.example/a.png") == 2


def test_estimate_stage_exits_on_empty_queue(collection, stub_fetcher, monkeypatch):
    monkeypatch.setattr(estimate, "sleep", lambda s: pytest.fail("slept without --daemon"))
    assert run_estimate(collection, stub_fetcher({})) == 0


def test_estimate_parse_args():
    args = estimate.parse_args([])
    assert (args.batch, args.limit, args.daemon, args.rescore) == (None, None, False, False)
    args = estimate.parse_args(["--rescore", "--batch", "4", "--limit", "10", "--daemon"])
    assert (args.batch, args.limit, args.daemon, args.rescore) == (4, 10, True, True)


@pytest.mark.parametrize("name, model_id", [(None, "738531d09237"), ("thesis", "27040e830f3f"),
                                            ("paper", "5727521cb771")])
def test_estimate_main_stores_the_checkpoint_id(collection, stub_fetcher, monkeypatch, name, model_id):
    fetcher = stub_fetcher(rescore_documents(collection))
    monkeypatch.delenv(labelqa.SHA256_ENV, raising=False)
    if name:
        monkeypatch.setenv(labelqa.MODEL_ENV, name)
    else:
        monkeypatch.delenv(labelqa.MODEL_ENV, raising=False)
    monkeypatch.setattr(estimate, "get_collection", lambda: collection)
    monkeypatch.setattr(estimate, "get_fetcher", lambda: fetcher)
    monkeypatch.setattr(estimate.torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(labelqa, "load_model", lambda device: scorer())
    assert estimate.main(["--rescore"]) == 0
    # every detected document, including those stored with MODEL_ID
    assert collection.count_documents({"label_quality_model": model_id}) == 6


def test_estimate_main_exits_on_a_checkpoint_error(collection, monkeypatch):
    def fail(device):
        raise labelqa.CheckpointError("cannot download the checkpoint from https://labelqa.example/x.pth: boom")

    monkeypatch.setattr(estimate, "get_collection", lambda: collection)
    monkeypatch.setattr(estimate.torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(labelqa, "load_model", fail)
    with pytest.raises(SystemExit, match="label-quality model: cannot download"):
        estimate.main([])


def test_estimate_main_exits_on_an_unknown_model(collection, monkeypatch):
    monkeypatch.setenv(labelqa.MODEL_ENV, "nope")
    monkeypatch.setattr(estimate, "get_collection", lambda: collection)
    monkeypatch.setattr(estimate.torch.cuda, "is_available", lambda: False)
    with pytest.raises(SystemExit, match="label-quality model: LABELQA_MODEL must be one of unified, thesis, paper"):
        estimate.main([])


def test_run_uses_the_model_batch_size_by_default(collection, stub_fetcher):
    collection.insert_many([{"url": f"https://img.example/{i}.png", "object_detection": {"cat": [[0, 0, 1, 1]]}}
                            for i in range(5)])
    network = StubNetwork()
    small = labelqa.Scorer(labelqa.Model(**{**vars(labelqa.MODELS["thesis"]), "batch": 2}), network, CPU)
    fetcher = stub_fetcher({f"https://img.example/{i}.png": png() for i in range(5)})
    assert estimate.run(collection, small, fetcher, MODEL_ID) == 5
    assert network.batches == [2, 2, 1]
