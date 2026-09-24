# Vendored from the label-quality-assurance repository,
# https://github.com/piculjantechnologies/label-quality-assurance
# thesis/data_loader.py at commit 07f31da. AGPL-3.0-only.
#
# The constants and functions below are copied verbatim; only the imports they
# need are kept.
"""Image and label preprocessing of the Pascal VOC checkpoint (see MODEL.md).

The image is an RGB uint8 array: resize_keep_aspect fits it into RES x RES and
normalize_image centre-pads it and applies the ImageNet MEAN and STD.
render_planes draws the boxes, given in the resized image's pixels, into one
RES x RES plane per class of VOC_CLASSES.
"""
import cv2
import numpy as np

VOC_CLASSES = ['aeroplane', 'bicycle', 'bird', 'boat', 'bottle', 'bus', 'car',
               'cat', 'chair', 'cow', 'diningtable', 'dog', 'horse',
               'motorbike', 'person', 'pottedplant', 'sheep', 'sofa', 'train',
               'tvmonitor']
MAPPING = {n: i for i, n in enumerate(VOC_CLASSES)}
NUM_CLASSES = len(VOC_CLASSES)

RES = 224

MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def resize_keep_aspect(image, size=RES):
    h, w = image.shape[:2]
    s = size / max(h, w)
    return cv2.resize(image, (max(1, int(w * s)), max(1, int(h * s))),
                      interpolation=cv2.INTER_NEAREST)


def center_pad(plane, size, channels):
    if channels == 1:
        out = np.zeros((size, size), dtype=np.float32)
    else:
        out = np.zeros((size, size, channels), dtype=np.float32)
    oy = (size - plane.shape[0]) // 2
    ox = (size - plane.shape[1]) // 2
    out[oy:oy + plane.shape[0], ox:ox + plane.shape[1]] = plane
    return out


def draw_box(plane, x1, y1, x2, y2):
    cv2.rectangle(plane, (x1, y1), (x2, y2), 1, 1)
    cv2.line(plane, (x1, y1), (x2, y2), 1, 1)
    cv2.line(plane, (x1, y2), (x2, y1), 1, 1)


def render_planes(bbs, plane_h, plane_w, size=RES):
    planes = np.zeros((NUM_CLASSES, size, size), dtype=np.float32)
    for key, boxes in bbs.items():
        plane = np.zeros((plane_h, plane_w), dtype=np.float32)
        for x1, y1, x2, y2 in boxes:
            draw_box(plane, int(x1), int(y1), int(x2), int(y2))
        planes[int(key)] = center_pad(plane, size, 1)
    return planes


def normalize_image(image, size=RES):
    img = center_pad(image / 255, size, 3)
    return ((img - MEAN) / STD).transpose(2, 0, 1)
