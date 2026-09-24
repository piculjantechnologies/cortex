"""What each label-quality model supplies, and the scorer the scoring stage uses (see MODEL.md)."""
from dataclasses import dataclass
from typing import Callable

import torch

# The 20 Pascal VOC class names the detection stage stores in object_detection.
VOC_CLASSES = ('aeroplane', 'bicycle', 'bird', 'boat', 'bottle', 'bus', 'car', 'cat', 'chair', 'cow',
               'diningtable', 'dog', 'horse', 'motorbike', 'person', 'pottedplant', 'sheep', 'sofa', 'train',
               'tvmonitor')


def known_boxes(object_detection):
    """{class name: boxes} for the Pascal VOC classes that have boxes; other class names are skipped."""
    return {name: boxes for name, boxes in object_detection.items() if name in VOC_CLASSES and boxes}


@dataclass(frozen=True)
class Model:
    """A checkpoint released in the label-quality-assurance repository and the code that scores with it.

    build turns the verified checkpoint bytes into the network (on the CPU).
    prepare turns a BGR uint8 image and its object_detection boxes (Pascal VOC
    class names to normalised [x1, y1, x2, y2]) into one sample of model
    inputs; forward runs the network on a list of samples and returns its
    logits, shape (N, 2), where output 1 is "good label".
    """
    name: str
    source: str
    url: str
    sha256: str
    file_name: str
    batch: int
    build: Callable
    prepare: Callable
    forward: Callable


class Scorer:
    """A loaded model: prepare() gives one sample of model inputs, score() the probabilities of a good label."""

    def __init__(self, model, network, device):
        self.model = model
        self.network = network
        self.device = device

    def prepare(self, bgr_image, object_detection):
        return self.model.prepare(bgr_image, object_detection)

    @torch.inference_mode()
    def score(self, samples):
        """P(good label) for each sample, in one model call; a (N,) CPU tensor in [0, 1]."""
        logits = self.model.forward(samples, self.network, self.device)
        return torch.nn.functional.softmax(logits.float(), dim=1)[:, 1].cpu()
