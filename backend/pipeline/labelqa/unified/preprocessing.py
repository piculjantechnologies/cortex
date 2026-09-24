# Vendored from the label-quality-assurance repository,
# https://github.com/piculjantechnologies/label-quality-assurance
# unified/qa_data.py at commit 07f31da. AGPL-3.0-only.
#
# The constants and functions below are copied verbatim (the training-data
# generator is left out). The repository's `import dataset as _dataset` is
# replaced by the VOC configuration in voc.py, and only the imports the code
# needs are kept.
"""Image and label preprocessing of the unified VOC checkpoint (see MODEL.md).

get_sample letterboxes a BGR uint8 image to RES x RES, normalises it (RGB,
ImageNet statistics) and draws each box (filled interior, border, both
diagonals) into its class's plane; box_rois gives the per-box head's RoI rows.
Boxes are [x1, y1, x2, y2] in original-image pixels, keyed by class name.
"""
import cv2
import numpy as np

from . import voc as _dataset

CLASSES = _dataset.CLASSES
MAPPING = {n: i for i, n in enumerate(CLASSES)}
NUM_CLASSES = len(CLASSES)

RES = _dataset.RES

IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

FILL = 0.25                    # interior value of a rendered box
BORDER_PX = 2                  # cv2 line thickness of the border (renders 3 px wide)


def resize_keep_aspect(image, size=RES):
    h, w = image.shape[:2]
    s = size / max(h, w)
    interp = cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR
    return cv2.resize(image, (max(1, int(w * s)), max(1, int(h * s))),
                      interpolation=interp)


def center_pad(plane, size, channels):
    shape = (size, size) if channels == 1 else (size, size, channels)
    out = np.zeros(shape, dtype=np.float32)
    oy = (size - plane.shape[0]) // 2
    ox = (size - plane.shape[1]) // 2
    out[oy:oy + plane.shape[0], ox:ox + plane.shape[1]] = plane
    return out


def normalize_image(image, size=RES):
    """image: BGR uint8 (OpenCV order) -> normalised CHW float32 in RGB order."""
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    img = center_pad(rgb.astype(np.float32) / 255.0, size, 3)
    return np.ascontiguousarray(
        ((img - IMAGENET_MEAN) / IMAGENET_STD).transpose(2, 0, 1))


def render_plane(boxes, plane_h, plane_w, size=RES):
    """One class plane: filled interior, thick border, both diagonals.

    Fills for every box are drawn before any border, so an overlapping
    box cannot erase a neighbour's border.
    """
    plane = np.zeros((plane_h, plane_w), dtype=np.float32)
    for x1, y1, x2, y2 in boxes:
        cv2.rectangle(plane, (int(x1), int(y1)), (int(x2), int(y2)),
                      FILL, -1)
    for x1, y1, x2, y2 in boxes:
        p1, p2 = (int(x1), int(y1)), (int(x2), int(y2))
        cv2.rectangle(plane, p1, p2, 1.0, BORDER_PX)
        cv2.line(plane, p1, p2, 1.0, 1)
        cv2.line(plane, (int(x1), int(y2)), (int(x2), int(y1)), 1.0, 1)
    return center_pad(plane, size, 1)


def render_planes(bbs, plane_h, plane_w, size=RES):
    """One plane per class (render_plane per non-empty class)."""
    planes = np.zeros((NUM_CLASSES, size, size), dtype=np.float32)
    for key, boxes in bbs.items():
        if boxes:
            planes[int(key)] = render_plane(boxes, plane_h, plane_w, size)
    return planes


def get_sample(image, annotation, res=RES):
    """Render one (BGR image, {class_name: [[x1,y1,x2,y2], ...]}) pair.

    Returns (img, planes), both CHW float32, matching the training transform.
    """
    old_h, old_w = image.shape[:2]
    image = resize_keep_aspect(image, res)
    new_h, new_w = image.shape[:2]
    bbs = {}
    for name, boxes in annotation.items():
        if name not in MAPPING:
            raise KeyError(f"unknown class {name!r}; "
                           f"expected one of {sorted(MAPPING)}")
        for x1, y1, x2, y2 in boxes:
            bbs.setdefault(MAPPING[name], []).append([
                int(np.clip(min(x1, x2) / old_w * new_w, 0, new_w - 1)),
                int(np.clip(min(y1, y2) / old_h * new_h, 0, new_h - 1)),
                int(np.clip(max(x1, x2) / old_w * new_w, 0, new_w - 1)),
                int(np.clip(max(y1, y2) / old_h * new_h, 0, new_h - 1))])
    return normalize_image(image, res), render_planes(bbs, new_h, new_w, res)


def box_rois(annotation, old_w, old_h, res=RES):
    """RoI rows for the per-box head from a {class_name: [[x1,y1,x2,y2]]}
    annotation, letterboxed exactly as get_sample renders: (N, 5)
    float32 rows [x1, y1, x2, y2, class_index]. Callers prepend the
    sample index column the model's `boxes` input expects.
    """
    s = res / max(old_w, old_h)
    new_w, new_h = max(1, int(old_w * s)), max(1, int(old_h * s))
    oy, ox = (res - new_h) // 2, (res - new_w) // 2
    rows = []
    for name, boxes in annotation.items():
        if name not in MAPPING:
            raise KeyError(f"unknown class {name!r}; "
                           f"expected one of {sorted(MAPPING)}")
        for x1, y1, x2, y2 in boxes:
            rows.append([
                int(np.clip(min(x1, x2) / old_w * new_w, 0, new_w - 1)) + ox,
                int(np.clip(min(y1, y2) / old_h * new_h, 0, new_h - 1)) + oy,
                int(np.clip(max(x1, x2) / old_w * new_w, 0, new_w - 1)) + ox,
                int(np.clip(max(y1, y2) / old_h * new_h, 0, new_h - 1)) + oy,
                MAPPING[name]])
    return np.array(rows, np.float32).reshape(-1, 5)
