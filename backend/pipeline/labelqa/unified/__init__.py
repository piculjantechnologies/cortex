"""The unified checkpoint: Pascal VOC, 224-px inputs (label-quality-assurance, unified/ folder).

network.py (FusionNet), preprocessing.py and voc.py are vendored from that
folder; the functions here feed them as unified/qa_demo.py (score) does. The
released VOC checkpoint (best_ema_calibrated.pth) is the one Cortex uses by
default. See MODEL.md.
"""
import io

import numpy as np
import torch

from ..base import Model, known_boxes
from .network import FusionNet
from .preprocessing import box_rois, get_sample


def annotation(object_detection, width, height):
    """{class name: [[x1, y1, x2, y2], ...]} in the pixels of a width x height image."""
    return {name: [[x1 * width, y1 * height, x2 * width, y2 * height] for x1, y1, x2, y2 in boxes]
            for name, boxes in known_boxes(object_detection).items()}


def prepare(bgr_image, object_detection):
    """(normalised image, label planes, RoI rows of the per-box head) for one image."""
    height, width = bgr_image.shape[:2]
    boxes = annotation(object_detection, width, height)
    image, planes = get_sample(bgr_image, boxes)
    return image, planes, box_rois(boxes, width, height)


def forward(samples, network, device):
    images = torch.from_numpy(np.stack([image for image, _, _ in samples])).float().to(device)
    planes = torch.from_numpy(np.stack([planes for _, planes, _ in samples])).float().to(device)
    boxes = None
    if network.per_box:
        # rows [sample, x1, y1, x2, y2, class], the sample index prepended as qa_demo.py does
        rows = [np.concatenate([np.full((len(rois), 1), i, np.float32), rois], 1)
                for i, (_, _, rois) in enumerate(samples)]
        boxes = torch.from_numpy(np.concatenate(rows)).to(device)
    return network(planes, images, boxes=boxes)


def build(data):
    """FusionNet with the configuration its state dict describes (FusionNet.from_checkpoint), loaded strictly.

    The bytes are first loaded with weights_only=True, which raises for any
    file that is not a plain state dict, so from_checkpoint's fallback to a
    full unpickle is never reached.
    """
    torch.load(io.BytesIO(data), map_location="cpu", weights_only=True)
    return FusionNet.from_checkpoint(io.BytesIO(data))


MODEL = Model(
    name="unified",
    source="unified/",
    url="https://labelqa.blob.core.windows.net/checkpoints/v1.0/unified__artifacts_voc__best_ema_calibrated.pth",
    sha256="738531d0923755983ac7fb697c48f5d40ef23083b3dc6c37f3f78ef4b616b757",
    file_name="best_ema_calibrated.pth",
    batch=32,
    build=build,
    prepare=prepare,
    forward=forward,
)
