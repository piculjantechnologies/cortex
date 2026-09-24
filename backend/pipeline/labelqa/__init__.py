"""Label-quality models: the three checkpoints released in the label-quality-assurance repository.

LABELQA_MODEL selects one: unified (the default; unified/ folder, Pascal VOC),
thesis (thesis/ folder, Pascal VOC) or paper (paper/ folder, COCO). Each
subpackage vendors the network and preprocessing of its model from that folder
and describes its checkpoint. The checkpoint (106 to 155 MB) is not part of
this repository: load_model() takes it from $LABELQA_CHECKPOINT, or from the
cache, where it is downloaded on first use, and loads only bytes whose SHA-256
matches. See MODEL.md.
"""
import hashlib
import logging
import os
import re
import tempfile
from pathlib import Path

import requests

from . import paper, thesis, unified
from .base import VOC_CLASSES, Model, Scorer, known_boxes

__all__ = ["VOC_CLASSES", "Model", "Scorer", "known_boxes", "CheckpointError", "MODELS", "selected",
           "expected_sha256", "model_id", "cache_path", "checkpoint_path", "load_model"]

log = logging.getLogger(__name__)

MODELS = {model.name: model for model in (unified.MODEL, thesis.MODEL, paper.MODEL)}
DEFAULT_MODEL = "unified"
MODEL_ENV = "LABELQA_MODEL"
CHECKPOINT_ENV = "LABELQA_CHECKPOINT"
URL_ENV = "LABELQA_CHECKPOINT_URL"
SHA256_ENV = "LABELQA_CHECKPOINT_SHA256"
DOWNLOAD_TIMEOUT = (10, 60)  # seconds to connect, and to wait for each read
DOWNLOAD_CHUNK = 1024 * 1024


class CheckpointError(RuntimeError):
    """The checkpoint cannot be read or downloaded, or its SHA-256 does not match."""


def selected():
    """The model $LABELQA_MODEL names (unified, thesis or paper; default unified)."""
    name = (os.environ.get(MODEL_ENV) or DEFAULT_MODEL).strip().lower()
    if name not in MODELS:
        raise CheckpointError(f"{MODEL_ENV} must be one of {', '.join(MODELS)}, not {name!r}")
    return MODELS[name]


def expected_sha256():
    """The SHA-256 the checkpoint must have: $LABELQA_CHECKPOINT_SHA256, else the selected model's."""
    value = (os.environ.get(SHA256_ENV) or selected().sha256).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise CheckpointError(f"{SHA256_ENV} must be 64 hexadecimal digits")
    return value


def model_id():
    """The label_quality_model value stored by the scoring stage: the checkpoint's SHA-256, first 12 hex digits."""
    return expected_sha256()[:12]


def cache_path(sha256):
    """Where the downloaded checkpoint is kept: $XDG_CACHE_HOME (default ~/.cache)/cortex/labelqa/.

    The file name is the hash's first 12 hex digits and the selected model's
    release file name, e.g. 738531d09237-best_ema_calibrated.pth.
    """
    base = os.environ.get("XDG_CACHE_HOME", "")
    if not os.path.isabs(base):
        base = Path.home() / ".cache"
    return Path(base) / "cortex" / "labelqa" / f"{sha256[:12]}-{selected().file_name}"


def checkpoint_path():
    """The checkpoint file: $LABELQA_CHECKPOINT, else the cache file, downloaded first when missing."""
    path = os.environ.get(CHECKPOINT_ENV)
    if path:
        return Path(path)
    sha256 = expected_sha256()
    path = cache_path(sha256)
    if not path.exists():
        download(os.environ.get(URL_ENV) or selected().url, path, sha256)
    return path


def download(url, path, sha256):
    """Stream `url` into a temporary file next to `path`; rename it to `path` once its SHA-256 matches."""
    log.info("Downloading the label-quality checkpoint from %s to %s", url, path)
    digest = hashlib.sha256()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".part")
    except OSError as exc:
        raise CheckpointError(f"cannot write the checkpoint to {path}: {exc}") from exc
    try:
        with os.fdopen(fd, "wb") as out:
            try:
                with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as response:
                    response.raise_for_status()
                    for chunk in response.iter_content(DOWNLOAD_CHUNK):
                        digest.update(chunk)
                        out.write(chunk)
            except requests.RequestException as exc:
                raise CheckpointError(f"cannot download the checkpoint from {url}: {exc}") from exc
            out.flush()
            os.fsync(out.fileno())
        if digest.hexdigest() != sha256:
            raise CheckpointError(
                f"the checkpoint downloaded from {url} has SHA-256 {digest.hexdigest()}, expected {sha256}; "
                "it was not saved")
        os.replace(tmp, path)
    except OSError as exc:
        Path(tmp).unlink(missing_ok=True)
        raise CheckpointError(f"cannot write the checkpoint to {path}: {exc}") from exc
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_verified(path, sha256):
    """The bytes of the file `path`; raises CheckpointError unless their SHA-256 is `sha256`."""
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise CheckpointError(f"cannot read the checkpoint {path}: {exc}") from exc
    actual = hashlib.sha256(data).hexdigest()
    if actual != sha256:
        raise CheckpointError(
            f"{path} has SHA-256 {actual}, expected {sha256}; set {SHA256_ENV} to load another checkpoint, "
            "or delete a damaged cache file to download it again")
    return data


def load_model(device):
    """The selected model's network, loaded from the verified checkpoint, frozen in eval mode on `device`.

    Returns a Scorer. The checkpoint comes from checkpoint_path(); the bytes
    that are loaded are the bytes whose SHA-256 was checked, and every model
    builds its network from them with torch.load(..., weights_only=True) and a
    strict load_state_dict. No weight is downloaded from torchvision: every
    weight comes from the checkpoint.
    """
    model = selected()
    path = checkpoint_path()
    data = read_verified(path, expected_sha256())
    log.info("Loading the %s label-quality checkpoint %s (label_quality_model %s)", model.name, path, model_id())
    network = model.build(data)
    network.to(device)
    network.eval()
    network.requires_grad_(False)
    return Scorer(model, network, device)
