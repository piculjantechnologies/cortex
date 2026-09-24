"""The thesis checkpoint: Pascal VOC, 224-px inputs (label-quality-assurance, thesis/ folder).

network.py (CorrNet) and preprocessing.py are vendored from that folder; the
functions here feed them as thesis/analysis.py (score) does. See MODEL.md.
"""
import io

import cv2
import numpy as np
import torch

from ..base import Model, known_boxes
from .network import CorrNet
from .preprocessing import MAPPING, normalize_image, render_planes, resize_keep_aspect


def to_pixels(boxes, width, height):
    """Normalised [x1, y1, x2, y2] boxes to pixels of a width x height image, clipped to it.

    As thesis/analysis.py of the label-quality-assurance repository converts
    boxes into the resized image's frame.
    """
    return {key: [[np.clip(x1 * width, 0, width - 1), np.clip(y1 * height, 0, height - 1),
                   np.clip(x2 * width, 0, width - 1), np.clip(y2 * height, 0, height - 1)]
                  for x1, y1, x2, y2 in lst]
            for key, lst in boxes.items()}


def prepare(bgr_image, object_detection):
    """(normalised image, label planes) for one image; the model takes RGB, as the thesis loader converts it."""
    image = resize_keep_aspect(cv2.cvtColor(bgr_image, cv2.COLOR_BGR2RGB))
    new_h, new_w = image.shape[:2]
    boxes = {MAPPING[name]: lst for name, lst in known_boxes(object_detection).items()}
    return normalize_image(image), render_planes(to_pixels(boxes, new_w, new_h), new_h, new_w)


def forward(samples, network, device):
    images = torch.from_numpy(np.stack([image for image, _ in samples])).float().to(device)
    labels = torch.from_numpy(np.stack([label for _, label in samples])).float().to(device)
    return network(images, labels)


def build(data):
    """CorrNet built from the state dict as thesis/neural_network.py's load_model builds it, loaded strictly.

    pretrained=False skips torchvision's ImageNet download: every weight comes
    from the checkpoint.
    """
    state = torch.load(io.BytesIO(data), map_location="cpu", weights_only=True)
    network = CorrNet(num_classes=state["model_2.0.weight"].shape[1], pretrained=False,
                      raster_head=any(k.startswith("rh_") for k in state))
    network.load_state_dict(state)
    return network


MODEL = Model(
    name="thesis",
    source="thesis/",
    url="https://labelqa.blob.core.windows.net/checkpoints/v1.0/thesis__artifacts__best_model_refit.pth",
    sha256="27040e830f3f927a6f4a797ff857140e5b33826a2129c74dacc60ca30f1590ca",
    file_name="best_model_refit.pth",
    batch=32,
    build=build,
    prepare=prepare,
    forward=forward,
)
