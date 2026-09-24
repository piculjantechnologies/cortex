"""pipeline.labelqa: model selection, checkpoint download and verification, the vendored models.

The golden values were computed with the reference code of the
label-quality-assurance repository at commit 07f31da and its released
checkpoints: thesis/data_loader.py (resize_keep_aspect, normalize_image,
render_planes), the box conversion of thesis/analysis.py and
thesis/neural_network.py's load_model; unified/qa_data.py (get_sample,
box_rois) and unified/qa_model.py's load_any, with QA_DATASET=voc; and
paper/data_loader.py (get_sample) and paper/coco_corrnet.py's
CorrNet.from_checkpoint.
"""
import hashlib
import os
import pickle
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("torchvision")
np = pytest.importorskip("numpy")
pytest.importorskip("cv2")
requests = pytest.importorskip("requests")
pytest.importorskip("pymongo")

from pipeline import detect_objects as detect, labelqa  # noqa: E402
from pipeline.labelqa import CheckpointError, paper, thesis, unified  # noqa: E402
from pipeline.labelqa.paper.network import CorrNet as PaperCorrNet  # noqa: E402
from pipeline.labelqa.thesis import preprocessing  # noqa: E402
from pipeline.labelqa.thesis.network import CorrNet  # noqa: E402
from pipeline.labelqa.unified.network import FusionNet  # noqa: E402

pytestmark = pytest.mark.pipeline

CPU = torch.device("cpu")
RELEASED = {
    "unified": "738531d0923755983ac7fb697c48f5d40ef23083b3dc6c37f3f78ef4b616b757",
    "thesis": "27040e830f3f927a6f4a797ff857140e5b33826a2129c74dacc60ca30f1590ca",
    "paper": "5727521cb7712774df8651e438dc70c1f08a91972bebd6e909d99f486012a9e4",
}
UNIFIED_SHA256 = RELEASED["unified"]
PAYLOAD = b"not a checkpoint " * 1000
PAYLOAD_SHA256 = hashlib.sha256(PAYLOAD).hexdigest()


# checkpoint resolution, download and verification

class FakeResponse:
    """The parts of a streamed requests.Response that labelqa.download uses."""

    def __init__(self, body, status=200, before_chunk=None):
        self.body = body
        self.status = status
        self.before_chunk = before_chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(f"{self.status} Client Error")

    def iter_content(self, chunk_size):
        for start in range(0, len(self.body), 1000):
            if self.before_chunk:
                self.before_chunk()
            yield self.body[start:start + 1000]


@pytest.fixture
def env(monkeypatch, tmp_path):
    """No LABELQA_* variables, the cache under tmp_path/cache, and any download fails the test."""
    for name in (labelqa.MODEL_ENV, labelqa.CHECKPOINT_ENV, labelqa.URL_ENV, labelqa.SHA256_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(labelqa.requests, "get", lambda *args, **kwargs: pytest.fail("unexpected download"))
    return tmp_path


@pytest.fixture
def serve(env, monkeypatch):
    """serve(body, status=200, before_chunk=None): requests.get answers with body; returns the recorded calls."""
    calls = []

    def install(body, status=200, before_chunk=None):
        def get(url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse(body, status, before_chunk)

        monkeypatch.setattr(labelqa.requests, "get", get)
        return calls

    return install


def test_the_three_released_models():
    assert list(labelqa.MODELS) == ["unified", "thesis", "paper"]
    assert labelqa.DEFAULT_MODEL == "unified"
    for name, model in labelqa.MODELS.items():
        assert model.name == name and model.source == f"{name}/"
        assert model.sha256 == RELEASED[name]
    assert labelqa.MODELS["unified"].url.endswith("/v1.0/unified__artifacts_voc__best_ema_calibrated.pth")
    assert labelqa.MODELS["thesis"].url.endswith("/v1.0/thesis__artifacts__best_model_refit.pth")
    assert labelqa.MODELS["paper"].url.endswith("/v1.0/paper__artifacts__best_model_refit.pth")
    assert [m.batch for m in labelqa.MODELS.values()] == [32, 32, 4]


def test_selected_model(env, monkeypatch):
    assert labelqa.selected() is labelqa.MODELS["unified"]
    for value, name in [("thesis", "thesis"), (" Paper\n", "paper"), ("UNIFIED", "unified"), ("", "unified")]:
        monkeypatch.setenv(labelqa.MODEL_ENV, value)
        assert labelqa.selected() is labelqa.MODELS[name]
    monkeypatch.setenv(labelqa.MODEL_ENV, "corrnet")
    with pytest.raises(CheckpointError, match="LABELQA_MODEL must be one of unified, thesis, paper, not 'corrnet'"):
        labelqa.selected()


@pytest.mark.parametrize("name", ["unified", "thesis", "paper"])
def test_expected_sha256_and_model_id(env, monkeypatch, name):
    monkeypatch.setenv(labelqa.MODEL_ENV, name)
    assert labelqa.expected_sha256() == RELEASED[name]
    assert labelqa.model_id() == RELEASED[name][:12]
    monkeypatch.setenv(labelqa.SHA256_ENV, f" {PAYLOAD_SHA256.upper()}\n")
    assert labelqa.expected_sha256() == PAYLOAD_SHA256
    assert labelqa.model_id() == PAYLOAD_SHA256[:12]
    monkeypatch.setenv(labelqa.SHA256_ENV, "27040e83")
    with pytest.raises(CheckpointError, match="64 hexadecimal digits"):
        labelqa.expected_sha256()


def test_cache_path_follows_xdg_cache_home(env, monkeypatch):
    name = "cortex/labelqa/738531d09237-best_ema_calibrated.pth"
    assert labelqa.cache_path(UNIFIED_SHA256) == env / "cache" / name
    monkeypatch.setenv("HOME", str(env / "home"))
    monkeypatch.setenv("XDG_CACHE_HOME", "relative/cache")  # ignored, as the XDG spec asks
    assert labelqa.cache_path(UNIFIED_SHA256) == env / "home/.cache" / name
    monkeypatch.delenv("XDG_CACHE_HOME")
    assert labelqa.cache_path(UNIFIED_SHA256) == env / "home/.cache" / name
    monkeypatch.setenv(labelqa.MODEL_ENV, "thesis")
    assert labelqa.cache_path(RELEASED["thesis"]).name == "27040e830f3f-best_model_refit.pth"
    monkeypatch.setenv(labelqa.MODEL_ENV, "paper")
    assert labelqa.cache_path(RELEASED["paper"]).name == "5727521cb771-best_model_refit.pth"


def test_env_path_is_used_without_download(env, monkeypatch):
    path = env / "mine.pth"
    monkeypatch.setenv(labelqa.CHECKPOINT_ENV, str(path))
    assert labelqa.checkpoint_path() == path
    with pytest.raises(CheckpointError, match="cannot read the checkpoint"):
        labelqa.load_model(CPU)


def test_cache_hit_is_not_downloaded_again(env):
    path = labelqa.cache_path(UNIFIED_SHA256)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"cached")
    assert labelqa.checkpoint_path() == path


def test_download_is_verified_before_the_atomic_rename(serve, monkeypatch):
    monkeypatch.setenv(labelqa.SHA256_ENV, PAYLOAD_SHA256)
    final = labelqa.cache_path(PAYLOAD_SHA256)
    during = []
    calls = serve(PAYLOAD, before_chunk=lambda: during.append((final.exists(), sorted(final.parent.iterdir()))))

    assert labelqa.checkpoint_path() == final
    assert final.read_bytes() == PAYLOAD
    # while streaming, only a temporary file next to the cache file exists
    assert len(during) == len(PAYLOAD) // 1000
    for exists, files in during:
        assert not exists
        assert len(files) == 1 and files[0].name.endswith(".part") and files[0].parent == final.parent
    assert list(final.parent.iterdir()) == [final]
    assert calls == [(labelqa.MODELS["unified"].url, {"stream": True, "timeout": labelqa.DOWNLOAD_TIMEOUT})]
    # the second call is a cache hit
    assert labelqa.checkpoint_path() == final
    assert len(calls) == 1


def test_download_url_can_be_overridden(serve, monkeypatch):
    monkeypatch.setenv(labelqa.SHA256_ENV, PAYLOAD_SHA256)
    monkeypatch.setenv(labelqa.URL_ENV, "https://mirror.example/checkpoint.pth")
    calls = serve(PAYLOAD)
    labelqa.checkpoint_path()
    assert [url for url, _ in calls] == ["https://mirror.example/checkpoint.pth"]


def test_download_with_the_wrong_sha256_saves_nothing(serve):
    serve(PAYLOAD)
    with pytest.raises(CheckpointError, match=f"has SHA-256 {PAYLOAD_SHA256}, expected {UNIFIED_SHA256}"):
        labelqa.checkpoint_path()
    assert list(labelqa.cache_path(UNIFIED_SHA256).parent.iterdir()) == []


@pytest.mark.parametrize("failure", ["http", "network"])
def test_failed_download_saves_nothing(serve, monkeypatch, failure):
    if failure == "http":
        serve(b"<Error/>", status=404)
    else:
        def get(url, **kwargs):
            raise requests.ConnectionError("connection refused")
        monkeypatch.setattr(labelqa.requests, "get", get)
    with pytest.raises(CheckpointError, match="cannot download the checkpoint from https://labelqa"):
        labelqa.checkpoint_path()
    assert list(labelqa.cache_path(UNIFIED_SHA256).parent.iterdir()) == []


@pytest.mark.parametrize("failure", ["mkstemp", "write"])
def test_cache_write_failure_is_a_checkpoint_error(serve, monkeypatch, failure):
    monkeypatch.setenv(labelqa.SHA256_ENV, PAYLOAD_SHA256)
    if failure == "mkstemp":
        def mkstemp(**kwargs):
            raise PermissionError(13, "Permission denied")
        monkeypatch.setattr(labelqa.tempfile, "mkstemp", mkstemp)
        serve(PAYLOAD)
    else:
        def disk_full():
            raise OSError(28, "No space left on device")
        serve(PAYLOAD, before_chunk=disk_full)
    with pytest.raises(CheckpointError, match="cannot write the checkpoint to .*best_ema_calibrated.pth"):
        labelqa.checkpoint_path()
    parent = labelqa.cache_path(PAYLOAD_SHA256).parent
    assert not parent.exists() or list(parent.iterdir()) == []


def test_load_model_never_loads_a_file_that_does_not_match(env, monkeypatch):
    path = env / "other.pth"
    torch.save({"x": torch.zeros(1)}, path)
    monkeypatch.setenv(labelqa.CHECKPOINT_ENV, str(path))
    monkeypatch.setattr(torch, "load", lambda *args, **kwargs: pytest.fail("loaded an unverified file"))
    with pytest.raises(CheckpointError, match=f"expected {UNIFIED_SHA256}"):
        labelqa.load_model(CPU)


def save_verified(env, monkeypatch, name, state):
    """Save `state` as the checkpoint of model `name`, with LABELQA_* pointing at it."""
    path = env / f"{name}.pth"
    torch.save(state, path)
    monkeypatch.setenv(labelqa.MODEL_ENV, name)
    monkeypatch.setenv(labelqa.CHECKPOINT_ENV, str(path))
    monkeypatch.setenv(labelqa.SHA256_ENV, hashlib.sha256(path.read_bytes()).hexdigest())
    return path


def assert_loaded(scorer, network_type, state):
    assert isinstance(scorer, labelqa.Scorer) and scorer.device == CPU
    network = scorer.network
    assert isinstance(network, network_type)
    assert not network.training
    assert not any(p.requires_grad for p in network.parameters())
    loaded = network.state_dict()
    assert loaded.keys() == state.keys()
    assert all(torch.equal(loaded[k], state[k]) for k in state)


def unified_network():
    return FusionNet(num_classes=20, fusion_res=28, input_res=224, per_box=True, pretrained_image=False)


def paper_network():
    return PaperCorrNet(corr_masked=True, corr_max=True, corr_cos=True, raster_head=True, input_norm=True)


@pytest.mark.parametrize("name, build, network_type", [
    ("thesis", lambda: CorrNet(pretrained=False, raster_head=True), CorrNet),
    ("paper", paper_network, PaperCorrNet),
    ("unified", unified_network, FusionNet),
])
def test_load_model_builds_the_network_from_the_state_dict_strictly(env, monkeypatch, name, build, network_type):
    torch.manual_seed(0)
    state = build().state_dict()
    path = save_verified(env, monkeypatch, name, state)

    scorer = labelqa.load_model(CPU)
    assert scorer.model is labelqa.MODELS[name]
    assert_loaded(scorer, network_type, state)

    state["unexpected"] = torch.zeros(1)
    torch.save(state, path)
    monkeypatch.setenv(labelqa.SHA256_ENV, hashlib.sha256(path.read_bytes()).hexdigest())
    with pytest.raises(RuntimeError, match="Unexpected key"):
        labelqa.load_model(CPU)


def test_loaded_networks_keep_their_configuration(env, monkeypatch):
    save_verified(env, monkeypatch, "thesis", CorrNet(pretrained=False, raster_head=False).state_dict())
    assert not labelqa.load_model(CPU).network.raster_head
    save_verified(env, monkeypatch, "unified", FusionNet(num_classes=20, fusion_res=28, input_res=224,
                                                         pretrained_image=False).state_dict())
    assert not labelqa.load_model(CPU).network.per_box
    save_verified(env, monkeypatch, "paper", PaperCorrNet().state_dict())
    network = labelqa.load_model(CPU).network
    assert not network.raster_head and not network.input_norm


@pytest.mark.parametrize("name", ["thesis", "paper", "unified"])
def test_a_checkpoint_that_is_not_a_plain_state_dict_is_refused(env, monkeypatch, name):
    save_verified(env, monkeypatch, name, {"weights": torch.zeros(1), "extra": Path("/")})
    monkeypatch.setattr(FusionNet, "from_checkpoint", lambda *args: pytest.fail("unpickled a non-weights file"))
    with pytest.raises(pickle.UnpicklingError):
        labelqa.load_model(CPU)


# preprocessing: golden values of the reference implementation

GOLDEN_BOXES = {"person": [[0.1, 0.2, 0.6, 0.9]], "dog": [[0.5, 0.5, 1.0, 1.0], [0.0, 0.0, 0.25, 0.3]],
                "car": [[-0.1, 0.4, 0.3, 1.2]]}


def golden_image():
    """A fixed 50 x 30 RGB image."""
    return (np.arange(30 * 50 * 3, dtype=np.uint32).reshape(30, 50, 3) * 37 % 256).astype(np.uint8)


def golden_bgr():
    """golden_image() in OpenCV's BGR order, as the scoring stage decodes images."""
    return np.ascontiguousarray(golden_image()[:, :, ::-1])


def sha256(array):
    return hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest()


def test_class_names_agree_with_the_detection_stage():
    assert set(labelqa.VOC_CLASSES) == detect.allowed_labels
    assert list(labelqa.VOC_CLASSES) == preprocessing.VOC_CLASSES == unified.preprocessing.CLASSES
    assert unified.voc.DETECTOR_NAME_MAP == detect.label_mappings
    assert paper.VOC_TO_COCO == {voc: coco for coco, voc in detect.label_mappings.items()}
    assert {n for n in labelqa.VOC_CLASSES if n not in paper.COCO_INDEX} == set(paper.VOC_TO_COCO)


def test_known_boxes_keeps_the_pascal_voc_classes_with_boxes():
    boxes = {"person": [[0, 0, 1, 1]], "tvmonitor": [[0.1, 0.1, 0.2, 0.2]], "giraffe": [[0, 0, 1, 1]], "cat": []}
    assert labelqa.known_boxes(boxes) == {"person": [[0, 0, 1, 1]], "tvmonitor": [[0.1, 0.1, 0.2, 0.2]]}
    assert labelqa.known_boxes({}) == {}
    assert labelqa.known_boxes({"giraffe": [[0, 0, 1, 1]]}) == {}


# thesis

# GOLDEN_BOXES in the pixels of the 224 x 134 resized image, clipped to it
THESIS_PIXELS = {14: [[22.400000000000002, 26.8, 134.4, 120.60000000000001]],
                 11: [[112.0, 67.0, 223.0, 133.0], [0.0, 0.0, 56.0, 40.199999999999996]],
                 6: [[0.0, 53.6, 67.2, 133.0]]}
THESIS_RESIZED_SHA256 = "95fe93abb2a1a0b3aa34ef223ba64d603f38d00be7674bc76c6033e99f72406f"
THESIS_IMAGE_SHA256 = "a4c5136bf36914d129d957b75a425619eb7f93f62a58c0d3e5cfb69882e02c38"
THESIS_PLANES_SHA256 = "7ac7681cb6e761f66ffb41ca90db4b47873dcc4cc10c7fe91e48c6dc64de63a5"


def test_thesis_constants():
    assert preprocessing.MAPPING == {name: i for i, name in enumerate(preprocessing.VOC_CLASSES)}
    assert preprocessing.NUM_CLASSES == 20 and preprocessing.RES == 224
    assert preprocessing.MEAN.tolist() == pytest.approx([0.485, 0.456, 0.406])  # RGB order
    assert preprocessing.STD.tolist() == pytest.approx([0.229, 0.224, 0.225])


def test_thesis_preprocessing_golden_values():
    resized = preprocessing.resize_keep_aspect(golden_image())
    assert resized.shape == (134, 224, 3)
    assert sha256(resized) == THESIS_RESIZED_SHA256

    image = preprocessing.normalize_image(resized)
    assert image.shape == (3, 224, 224) and image.dtype == np.float32
    assert sha256(image) == THESIS_IMAGE_SHA256
    # the padding (45 rows above the image) is black: -MEAN / STD after normalisation
    assert image[:, 0, 0].tolist() == pytest.approx((-preprocessing.MEAN / preprocessing.STD).tolist())

    planes = preprocessing.render_planes(THESIS_PIXELS, 134, 224)
    assert planes.shape == (20, 224, 224) and planes.dtype == np.float32
    assert sha256(planes) == THESIS_PLANES_SHA256
    assert {k: float(planes[k].sum()) for k in range(20) if planes[k].any()} == {6: 451.0, 11: 873.0, 14: 633.0}
    assert planes[:, :45].max() == 0.0 and planes[:, 45 + 134:].max() == 0.0


def test_thesis_boxes_are_converted_to_resized_pixels_and_clipped():
    boxes = {preprocessing.MAPPING[name]: lst for name, lst in GOLDEN_BOXES.items()}
    assert thesis.to_pixels(boxes, 224, 134) == THESIS_PIXELS


def test_thesis_prepare_matches_the_golden_values():
    image, planes = thesis.prepare(golden_bgr(), GOLDEN_BOXES)
    assert sha256(image) == THESIS_IMAGE_SHA256
    assert sha256(planes) == THESIS_PLANES_SHA256
    _, without_unknown = thesis.prepare(golden_bgr(), {**GOLDEN_BOXES, "giraffe": [[0.2, 0.2, 0.4, 0.4]]})
    assert sha256(without_unknown) == THESIS_PLANES_SHA256


# unified

UNIFIED_IMAGE_SHA256 = "be0508269141fd9d19c326dd12af49883a205106f0b9563f475a62b3bd7f814e"
UNIFIED_PLANES_SHA256 = "a4e4dd2d93f52268b6c987cf08e0bce1d7b67bb2c24216c1036f1f258cdca80c"
UNIFIED_ROIS = [[22.0, 71.0, 134.0, 165.0, 14.0], [112.0, 112.0, 223.0, 178.0, 11.0],
                [0.0, 45.0, 56.0, 85.0, 11.0], [0.0, 98.0, 67.0, 178.0, 6.0]]


def test_unified_constants():
    pre = unified.preprocessing
    assert pre.MAPPING == {name: i for i, name in enumerate(pre.CLASSES)}
    assert pre.NUM_CLASSES == 20 and pre.RES == 224 and unified.voc.FUSION_RES == 28
    assert pre.IMAGENET_MEAN.tolist() == pytest.approx([0.485, 0.456, 0.406])
    assert pre.IMAGENET_STD.tolist() == pytest.approx([0.229, 0.224, 0.225])
    assert (pre.FILL, pre.BORDER_PX) == (0.25, 2)


def test_unified_annotation_is_in_original_image_pixels():
    assert unified.annotation({"cat": [[0.1, 0.2, 0.5, 1.0]], "giraffe": [[0, 0, 1, 1]], "dog": []}, 50, 30) == {
        "cat": [[5.0, 6.0, 25.0, 30.0]]}


def test_unified_prepare_matches_the_golden_values():
    image, planes, rois = unified.prepare(golden_bgr(), GOLDEN_BOXES)
    assert image.shape == (3, 224, 224) and image.dtype == np.float32
    assert planes.shape == (20, 224, 224) and planes.dtype == np.float32
    assert sha256(image) == UNIFIED_IMAGE_SHA256
    assert sha256(planes) == UNIFIED_PLANES_SHA256
    assert {k: float(planes[k].sum()) for k in range(20) if planes[k].any()} == {6: 2075.75, 11: 3777.5, 14: 3874.5}
    assert rois.dtype == np.float32 and rois.tolist() == UNIFIED_ROIS
    _, without_unknown, rois = unified.prepare(golden_bgr(), {**GOLDEN_BOXES, "giraffe": [[0.2, 0.2, 0.4, 0.4]]})
    assert sha256(without_unknown) == UNIFIED_PLANES_SHA256 and rois.tolist() == UNIFIED_ROIS


def test_unified_forward_passes_the_boxes_of_every_sample():
    class Recorder(torch.nn.Module):
        per_box = True

        def forward(self, planes, images, boxes=None):
            self.calls = (planes.shape, images.shape, boxes)
            return torch.zeros(planes.shape[0], 2)

    network = Recorder()
    samples = [unified.prepare(golden_bgr(), GOLDEN_BOXES), unified.prepare(golden_bgr(), {"cat": [[0, 0, 1, 1]]})]
    unified.forward(samples, network, CPU)
    planes_shape, images_shape, boxes = network.calls
    assert planes_shape == (2, 20, 224, 224) and images_shape == (2, 3, 224, 224)
    assert boxes.tolist() == [[0.0, *row] for row in UNIFIED_ROIS] + [[1.0, 0.0, 45.0, 223.0, 178.0, 7.0]]
    network.per_box = False
    unified.forward(samples, network, CPU)
    assert network.calls[2] is None


# paper

PAPER_ANNOTATION = {0: [[5.0, 6.0, 25.0, 21.0]], 16: [[25.0, 15.0, 25.0, 15.0], [0.0, 0.0, 12.5, 9.0]],
                    2: [[-5.0, 12.0, 20.0, 23.999999999999996]]}
PAPER_CATS_SHA256 = "6d6a7bd06f07bcc598f3c5041f7b72ba685c175d1c987602348b87d88d53e340"
PAPER_BACKGROUND_SHA256 = "0dd8a36cb126223ac7b32dd401d41f1bb2e9ecba6b0e2965ce4dadad2527a175"


def test_paper_constants():
    assert len(paper.COCO_CLASSES) == paper.preprocessing.NUM_CLASSES == 80
    assert paper.COCO_INDEX["person"] == 0 and paper.COCO_INDEX["toothbrush"] == 79
    assert paper.SIZE == 640


def test_paper_annotation_maps_pascal_voc_names_to_coco_boxes_in_pixels():
    assert paper.annotation(GOLDEN_BOXES, 50, 30) == pytest.approx(PAPER_ANNOTATION)
    renamed = paper.annotation({name: [[0.0, 0.0, 0.5, 0.5]] for name in paper.VOC_TO_COCO}, 10, 10)
    assert sorted(renamed) == sorted(paper.COCO_INDEX[coco] for coco in paper.VOC_TO_COCO.values())
    assert paper.annotation({"giraffe": [[0, 0, 1, 1]], "cat": []}, 10, 10) == {}


def test_paper_prepare_matches_the_golden_values():
    cats, background = paper.prepare(golden_bgr(), GOLDEN_BOXES)
    assert cats.shape == (80, 640, 640) and background.shape == (3, 640, 640)
    assert sha256(cats) == PAPER_CATS_SHA256
    assert sha256(background) == PAPER_BACKGROUND_SHA256
    assert {k: float(cats[k].sum()) for k in range(80) if cats[k].any()} == {0: 1178.0, 2: 844.0, 16: 1570.0}
    without_unknown, _ = paper.prepare(golden_bgr(), {**GOLDEN_BOXES, "giraffe": [[0.2, 0.2, 0.4, 0.4]]})
    assert sha256(without_unknown) == PAPER_CATS_SHA256


# the released checkpoints (each skipped unless it is on this machine)

GOLDEN_P_GOOD = {"unified": 0.005759070161730051, "thesis": 0.0019045714288949966, "paper": 0.0733703002333641}
TENSORS = {"unified": 489, "thesis": 285, "paper": 288}


def released_checkpoint(name):
    """The model's cache file, or $LABELQA_CHECKPOINT when LABELQA_MODEL names this model; None if missing."""
    path = None
    if os.environ.get(labelqa.MODEL_ENV, labelqa.DEFAULT_MODEL).strip().lower() == name:
        path = os.environ.get(labelqa.CHECKPOINT_ENV)
    if not path:
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv(labelqa.MODEL_ENV, name)
            path = labelqa.cache_path(RELEASED[name])
    path = Path(path)
    return path if path.is_file() else None


@pytest.fixture(scope="module", params=["unified", "thesis", "paper"])
def released(request):
    name = request.param
    path = released_checkpoint(name)
    if path is None:
        pytest.skip(f"released {name} checkpoint not available: set LABELQA_CHECKPOINT or run the scoring stage once")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(labelqa.MODEL_ENV, name)
        mp.setenv(labelqa.CHECKPOINT_ENV, str(path))
        mp.delenv(labelqa.SHA256_ENV, raising=False)
        mp.setattr(labelqa.requests, "get", lambda *args, **kwargs: pytest.fail("unexpected download"))
        return labelqa.load_model(CPU)


def test_released_checkpoint_loads_strictly_in_eval_mode(released):
    network = released.network
    assert len(network.state_dict()) == TENSORS[released.model.name]
    assert not network.training
    assert not any(p.requires_grad for p in network.parameters())
    if released.model.name == "unified":
        assert network.per_box and type(network.image_trunk.layer1[0]).__name__ == "Bottleneck"  # ResNet-50
    else:
        assert network.raster_head


def test_released_checkpoint_golden_score(released):
    score = released.score([released.prepare(golden_bgr(), GOLDEN_BOXES)])[0].item()
    assert score == pytest.approx(GOLDEN_P_GOOD[released.model.name], abs=1e-5)


def test_released_checkpoint_batch_equals_single_scores(released):
    samples = [released.prepare(golden_bgr(), GOLDEN_BOXES),
               released.prepare(np.full((90, 60, 3), 127, np.uint8),
                                {"person": [[0.0, 0.0, 1.0, 1.0]], "car": [[0.1, 0.5, 0.3, 0.7]]})]
    scores = released.score(samples)
    assert scores.shape == (2,) and scores.requires_grad is False
    assert ((scores >= 0) & (scores <= 1)).all()
    assert released.score(samples[1:])[0].item() == pytest.approx(scores[1].item(), abs=1e-5)
