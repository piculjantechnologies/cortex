"""The paper checkpoint: COCO, 80 classes, 640-px inputs (label-quality-assurance, paper/ folder).

network.py (CorrNet) and preprocessing.py are vendored from that folder; the
functions here feed them as paper/web_demo.py (classify) does. Cortex's boxes
carry Pascal VOC class names, all 20 of which are COCO classes: six are
renamed to their COCO spelling. See MODEL.md.
"""
import io

import numpy as np
import torch

from ..base import Model, known_boxes
from .network import CorrNet
from .preprocessing import COCO_CLASSES, get_sample

SIZE = 640  # the input size paper/web_demo.py and paper/demo_samples.py render at
COCO_INDEX = {name: i for i, name in enumerate(COCO_CLASSES)}
# The Pascal VOC names whose COCO spelling differs; the other 14 are the same.
VOC_TO_COCO = {'aeroplane': 'airplane', 'diningtable': 'dining table', 'motorbike': 'motorcycle',
               'pottedplant': 'potted plant', 'sofa': 'couch', 'tvmonitor': 'tv'}


def annotation(object_detection, width, height):
    """{COCO class index: [[x, y, w, h], ...]} in the pixels of a width x height image."""
    return {COCO_INDEX[VOC_TO_COCO.get(name, name)]:
            [[x1 * width, y1 * height, (x2 - x1) * width, (y2 - y1) * height] for x1, y1, x2, y2 in boxes]
            for name, boxes in known_boxes(object_detection).items()}


def prepare(bgr_image, object_detection):
    """(label planes, letterboxed image) for one image, as get_sample returns them."""
    height, width = bgr_image.shape[:2]
    return get_sample(SIZE, bgr_image, annotation(object_detection, width, height))


def forward(samples, network, device):
    cats = torch.from_numpy(np.stack([cats for cats, _ in samples])).float().to(device)
    background = torch.from_numpy(np.stack([background for _, background in samples])).float().to(device)
    return network(cats, background)


def build(data):
    """CorrNet with the configuration recorded in the checkpoint (CorrNet.from_checkpoint), loaded strictly."""
    state = torch.load(io.BytesIO(data), map_location="cpu", weights_only=True)
    return CorrNet.from_checkpoint(state)


MODEL = Model(
    name="paper",
    source="paper/",
    url="https://labelqa.blob.core.windows.net/checkpoints/v1.0/paper__artifacts__best_model_refit.pth",
    sha256="5727521cb7712774df8651e438dc70c1f08a91972bebd6e909d99f486012a9e4",
    file_name="best_model_refit.pth",
    batch=4,
    build=build,
    prepare=prepare,
    forward=forward,
)
