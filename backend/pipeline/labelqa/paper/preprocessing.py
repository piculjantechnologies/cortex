# Vendored from the label-quality-assurance repository,
# https://github.com/piculjantechnologies/label-quality-assurance
# paper/interactive_demo.py (COCO_CLASSES) and paper/data_loader.py
# (NUM_CLASSES, get_sample) at commit 07f31da. AGPL-3.0-only.
#
# The constants and the function below are copied verbatim; only the imports
# they need are kept.
"""Image and label preprocessing of the paper's COCO checkpoint (see MODEL.md).

get_sample letterboxes a BGR uint8 image to size x size (unnormalised; the
network normalises it) and draws each box's 1-px border into its class's plane
of COCO_CLASSES (boxes as [x, y, w, h] in original-image pixels).
"""
import cv2
import numpy as np

COCO_CLASSES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard",
    "sports ball", "kite", "baseball bat", "baseball glove", "skateboard",
    "surfboard", "tennis racket", "bottle", "wine glass", "cup", "fork",
    "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
    "couch", "potted plant", "bed", "dining table", "toilet", "tv",
    "laptop", "mouse", "remote", "keyboard", "cell phone", "microwave",
    "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
    "scissors", "teddy bear", "hair drier", "toothbrush",
]

NUM_CLASSES = 80


def get_sample(size, image, annotation, fill=0.0, border_px=1):
    """Render one (image, annotation) pair as model inputs.

    image: BGR uint8. annotation: {class_index: [[x, y, w, h], ...]} in
    original-image pixels. The image is resized keeping its aspect ratio
    and padded with black to size x size; each box border is drawn with
    value 1 in its class channel. The defaults are the paper rendering
    (1 px hollow border); fill > 0 additionally paints the interior at
    that value and border_px sets the border thickness (options outside
    the release recipe; train_coco_corr.py uses the defaults). Returns
    (cats, background), both CHW float32.
    """
    h, w = image.shape[:2]
    s = size / max(h, w)
    nw, nh = max(1, int(w * s)), max(1, int(h * s))
    ox, oy = (size - nw) // 2, (size - nh) // 2
    background = np.zeros((size, size, 3), np.float32)
    background[oy:oy + nh, ox:ox + nw] = cv2.resize(image, (nw, nh))
    cats = np.zeros((NUM_CLASSES, size, size), np.float32)
    for cls, boxes in annotation.items():
        for x, y, bw, bh in boxes:
            xa, xb = sorted((x, x + bw))
            ya, yb = sorted((y, y + bh))
            x1 = int(np.clip(xa * s, 0, nw - 1)) + ox
            y1 = int(np.clip(ya * s, 0, nh - 1)) + oy
            x2 = int(np.clip(xb * s, 0, nw - 1)) + ox
            y2 = int(np.clip(yb * s, 0, nh - 1)) + oy
            if x2 > x1 and y2 > y1:
                if fill > 0:
                    cv2.rectangle(cats[int(cls)], (x1, y1), (x2, y2),
                                  fill, -1)
                cv2.rectangle(cats[int(cls)], (x1, y1), (x2, y2), 1,
                              border_px)
    return cats, background.transpose(2, 0, 1)
