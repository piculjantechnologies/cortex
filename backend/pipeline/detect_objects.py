"""Detection stage: detect Pascal-VOC objects in the images the ingest stage stored.

For each document without object_detection it downloads the image, runs
SSDlite320-MobileNetV3 (COCO) and stores the boxes of the 20 VOC classes
(normalised x1, y1, x2, y2), per-class box counts and largest box areas
(object_stats), the image width and height, and the sha256 of the image bytes
as `hash`. Images that cannot be fetched or decoded (animated ones
included) are marked "error: broken image", images wider or taller than
8192 px "error: too large". A document whose host asked the stage to wait
(http.RetryLater) is left in the queue for a later round.

    python -m pipeline.detect_objects [--batch 8] [--limit N] [--daemon]
"""
import argparse
import hashlib
import io
import logging
import warnings
from time import sleep

import torch
from PIL import Image
from torchvision.io import ImageReadMode, decode_image
from torchvision.models.detection import SSDLite320_MobileNet_V3_Large_Weights, ssdlite320_mobilenet_v3_large

from .config import positive_int
from .db import draw, get_collection, has_requests
from .http import MAX_IMAGE_BYTES, FetchError, RetryLater, get_fetcher
from .object_stats import object_stats

log = logging.getLogger(__name__)

BROKEN_IMAGE = "error: broken image"
TOO_LARGE = "error: too large"
MAX_SIDE = 8192
SCORE_THRESHOLD = 0.9
SAMPLE_SIZE = 1000  # documents drawn from the queue per round
IDLE_SLEEP = 60  # seconds between polls of an empty queue in --daemon mode
QUEUE = {"object_detection": {"$exists": False}}
HEADER_FORMATS = ("JPEG", "PNG", "GIF", "WEBP")  # the formats decode_image reads

# Label mapping
label_mappings = {
    'motorcycle': 'motorbike', 'airplane': 'aeroplane', 'couch': 'sofa',
    'potted plant': 'pottedplant', 'dining table': 'diningtable', 'tv': 'tvmonitor'
}
allowed_labels = {'aeroplane', 'bicycle', 'bird', 'boat', 'bottle', 'bus', 'car',
                  'cat', 'chair', 'cow', 'diningtable', 'dog', 'horse',
                  'motorbike', 'person', 'pottedplant', 'sheep', 'sofa', 'train',
                  'tvmonitor'}


def load_model(device):
    """The COCO detector, its weights enum (transforms + category names) on `device`."""
    weights = SSDLite320_MobileNet_V3_Large_Weights.DEFAULT
    model = ssdlite320_mobilenet_v3_large(weights=weights).eval().to(device)
    return model, weights


class ImageTooLarge(ValueError):
    """The image is wider or taller than MAX_SIDE pixels."""


def check_size(content):
    """Raise ImageTooLarge when the image header declares a side over MAX_SIDE.

    Only the header is parsed, so a small file that would decode to a huge
    bitmap is refused before any pixels are allocated. Data Pillow cannot
    identify is left to decode_image.
    """
    try:
        with warnings.catch_warnings():
            # Pillow warns above ~89 Mpx; such an image is too large anyway.
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content), formats=HEADER_FORMATS) as image:
                width, height = image.size
    except Image.DecompressionBombError as exc:
        raise ImageTooLarge(str(exc)) from exc
    except (OSError, ValueError):
        return
    if max(width, height) > MAX_SIDE:
        raise ImageTooLarge(f"{width} x {height} px")


def decode(content):
    """Decode image bytes to a (3, H, W) RGB tensor with EXIF orientation applied.

    The tensor is uint8, or uint16 for a 16-bit PNG (the detector's transforms
    accept both). Grayscale, palette, RGBA and CMYK images are converted to RGB. Raises
    ImageTooLarge (a ValueError) for an image over MAX_SIDE pixels, and
    RuntimeError or ValueError for data that is not a decodable single-frame
    image.
    """
    check_size(content)
    data = torch.frombuffer(bytearray(content), dtype=torch.uint8)
    tensor = decode_image(data, mode=ImageReadMode.RGB, apply_exif_orientation=True)
    if tensor.ndim != 3:  # an animated GIF decodes to (frames, 3, H, W)
        raise ValueError("not a single-frame image")
    if max(tensor.shape[1:]) > MAX_SIDE:
        raise ImageTooLarge(f"{tensor.shape[2]} x {tensor.shape[1]} px")
    return tensor


def read_image_from_url(url, fetcher):
    """Fetch and decode `url`; return (image tensor, sha256 hex digest of the bytes)."""
    content = fetcher.fetch(url, max_bytes=MAX_IMAGE_BYTES).content
    return decode(content), hashlib.sha256(content).hexdigest()


@torch.inference_mode()
def detect_objects(images, model, preprocess, device):
    """Run the detector on a batch of (3, H, W) images in one model call.

    Returns one (scores, boxes, labels, width, height) per image, keeping
    detections scored >= 0.9 with boxes in original-image pixels.
    """
    batch = [preprocess(image).to(device) for image in images]
    predictions = model(batch)
    results = []
    for image, preprocessed_tensor, prediction in zip(images, batch, predictions):
        # Get the dimensions before and after preprocessing
        original_height, original_width = image.shape[1], image.shape[2]
        preprocessed_height, preprocessed_width = preprocessed_tensor.shape[1], preprocessed_tensor.shape[2]
        height_scale = original_height / preprocessed_height
        width_scale = original_width / preprocessed_width

        scores = prediction['scores'].cpu().numpy()
        boxes = prediction['boxes'].cpu().numpy()
        labels = prediction['labels'].cpu().numpy()

        # Filter out objects with scores below 0.9
        high_score_indices = scores >= SCORE_THRESHOLD
        scores = scores[high_score_indices]
        boxes = boxes[high_score_indices]
        labels = labels[high_score_indices]

        # Rescale boxes to original image dimensions
        boxes[:, [0, 2]] *= width_scale  # Adjust x1 and x2
        boxes[:, [1, 3]] *= height_scale  # Adjust y1 and y2

        results.append((scores, boxes, labels, original_width, original_height))
    return results


def build_object_detection(boxes, labels, image_width, image_height, categories):
    """Map COCO detections to {voc_label: [[x1, y1, x2, y2], ...]} normalised to [0, 1]."""
    object_detection = {}
    for box, label in zip(boxes, labels):
        # Get the label name, apply mappings if available, otherwise use the label name directly
        label_name = categories[label]
        label_name = label_mappings.get(label_name, label_name)
        if label_name not in allowed_labels:
            continue

        x1, y1, x2, y2 = box
        object_detection.setdefault(label_name, []).append([
            float(x1 / image_width), float(y1 / image_height),
            float(x2 / image_width), float(y2 / image_height),
        ])
    return object_detection


def process_batch(documents, collection, model, preprocess, categories, device, fetcher):
    """Fetch, decode and detect one batch of queued documents and store the results.

    Returns the number of documents stored; those whose host asked to wait
    stay in the queue.
    """
    ready = []
    stored = 0
    for document in documents:
        url = document.get("url")
        try:
            image_tensor, digest = read_image_from_url(url, fetcher)
        except RetryLater as e:
            log.info("Leaving %s in the queue: %s", url, e)
            continue
        except ImageTooLarge as e:
            log.info("Image %s is too large: %s", url, e)
            collection.update_one({"_id": document["_id"]}, {"$set": {"object_detection": TOO_LARGE}})
            stored += 1
            continue
        except (FetchError, RuntimeError, ValueError) as e:
            log.info("Error loading image from URL %s: %s", url, e)
            collection.update_one({"_id": document["_id"]}, {"$set": {"object_detection": BROKEN_IMAGE}})
            stored += 1
            continue
        ready.append((document, image_tensor, digest))
    if not ready:
        return stored

    results = detect_objects([image for _, image, _ in ready], model, preprocess, device)
    for (document, _, digest), (_, boxes, labels, image_width, image_height) in zip(ready, results):
        object_detection = build_object_detection(boxes, labels, image_width, image_height, categories)
        collection.update_one(
            {"_id": document["_id"]},
            {"$set": {
                "object_detection": object_detection,
                **object_stats(object_detection),
                "width": image_width,
                "height": image_height,
                "hash": digest,
            }},
        )
    return stored + len(ready)


def run(collection, model, preprocess, categories, device, fetcher, batch=8, limit=None, daemon=False):
    """Process queued documents until the queue is empty (or `limit` is reached).

    A round in which every document drawn has to wait for its host ends the
    run too. With `daemon`, the queue is polled again every IDLE_SLEEP
    seconds instead. Returns the number of documents stored.
    """
    processed = 0
    while limit is None or processed < limit:
        size = SAMPLE_SIZE if limit is None else min(SAMPLE_SIZE, limit - processed)
        documents = draw(collection, QUEUE, size)
        stored = 0
        for start in range(0, len(documents), batch):
            chunk = documents[start:start + batch]
            stored += process_batch(chunk, collection, model, preprocess, categories, device, fetcher)
            log.info("%d documents processed", processed + stored)
            # A user request that arrived during the round starts a new round, so it waits
            # for at most one batch instead of the rest of the round.
            following = documents[start + batch:start + batch + 1]
            if following and "requested_at" not in following[0] and has_requests(collection, QUEUE):
                break
        processed += stored
        if not stored:
            if not daemon:
                if documents:
                    log.info("%d queued documents are waiting for their hosts; run again later", len(documents))
                else:
                    log.info("No documents left to detect")
                break
            sleep(IDLE_SLEEP)
    return processed


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Detect Pascal-VOC objects in queued images and store boxes, size and hash.")
    parser.add_argument("--batch", type=positive_int, default=8,
                        help="images per model call (default: 8)")
    parser.add_argument("--limit", type=positive_int,
                        help="stop after this many documents (default: until the queue is empty)")
    parser.add_argument("--daemon", action="store_true",
                        help=f"poll an empty (or waiting) queue every {IDLE_SLEEP}s instead of exiting")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    collection = get_collection()
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    log.info("Using device: %s", device)
    model, weights = load_model(device)
    run(collection, model, weights.transforms(), weights.meta["categories"], device, get_fetcher(),
        batch=args.batch, limit=args.limit, daemon=args.daemon)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
