"""Scoring stage: estimate how well the detected boxes fit each image.

For each document with object_detection boxes and no label_quality_score it
downloads the image, scores the boxes with the label-quality model that
LABELQA_MODEL selects (unified by default; pipeline/labelqa, see MODEL.md) and
stores the probability as label_quality_score, together with
label_quality_model (the first 12 hex digits of the checkpoint's SHA-256). When a document cannot be scored it stores
label_quality_score = null plus label_quality_error, so it leaves the queue;
documents without boxes of a Pascal VOC class ({}) are stored the same way. A
document whose host asked the stage to wait (http.RetryLater) is left in the
queue for a later round. With --rescore the queue also holds every document
whose label_quality_model is not the current one (documents scored without the
field included), so their scores are recomputed.

    python -m pipeline.estimate_label_quality [--batch N] [--limit N] [--daemon] [--rescore]
"""
import argparse
import logging
from time import sleep

import cv2
import numpy as np
import torch

from . import labelqa
from .config import positive_int
from .db import draw, get_collection, has_requests
from .http import MAX_IMAGE_BYTES, FetchError, RetryLater, get_fetcher

log = logging.getLogger(__name__)

SAMPLE_SIZE = 1000  # documents drawn from the queue per round
IDLE_SLEEP = 60  # seconds between polls of an empty queue in --daemon mode
# object_detection is a sub-document (boxes per class, possibly {}); the
# "error: ..." strings of the detection stage are excluded.
QUEUE = {"label_quality_score": {"$exists": False}, "object_detection": {"$type": "object"}}


def queue(model_id, rescore=False):
    """The query selecting the documents to score.

    QUEUE (the documents without a score), or with `rescore` every document
    with an object_detection sub-document whose label_quality_model is not
    `model_id`: QUEUE's documents, and those stored by another checkpoint or
    before the field was stored.
    """
    if not rescore:
        return QUEUE
    return {"object_detection": {"$type": "object"}, "label_quality_model": {"$ne": model_id}}


def get_image_from_url(url, fetcher):
    """Fetch an image and decode it to a BGR uint8 array; raises FetchError or ValueError.

    OpenCV decodes with the EXIF orientation applied; each model's prepare()
    takes this BGR array, as the label-quality-assurance code reads images with
    OpenCV.
    """
    content = fetcher.fetch(url, max_bytes=MAX_IMAGE_BYTES).content
    image = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Failed to decode image.")
    return image


def get_label_quality_score(bgr_image, object_detection, scorer):
    """The label quality score (probability that the boxes are correct) for one image."""
    return scorer.score([scorer.prepare(bgr_image, object_detection)])[0].item()


def _mark_unscored(collection, document, reason, model_id):
    collection.update_one(
        {"_id": document["_id"]},
        {"$set": {"label_quality_score": None, "label_quality_error": reason[:300], "label_quality_model": model_id}},
    )


def process_batch(documents, collection, scorer, fetcher, model_id):
    """Fetch and score one batch of queued documents and store the results.

    Returns the number of documents stored; those whose host asked to wait
    stay in the queue.
    """
    ready, samples = [], []
    stored = 0
    for document in documents:
        url = document.get("url")
        object_detection = document["object_detection"]
        if not labelqa.known_boxes(object_detection):
            _mark_unscored(collection, document, "no detections", model_id)
            stored += 1
            continue
        try:
            image = get_image_from_url(url, fetcher)
            samples.append(scorer.prepare(image, object_detection))
        except RetryLater as e:
            log.info("Leaving %s in the queue: %s", url, e)
            continue
        except FetchError as e:
            log.info("Failed to fetch image from URL %s: %s", url, e)
            _mark_unscored(collection, document, f"fetch: {e}", model_id)
            stored += 1
            continue
        except (ValueError, KeyError, TypeError, IndexError, cv2.error) as e:
            log.info("Error processing image %s: %s", url, e)
            _mark_unscored(collection, document, f"{type(e).__name__}: {e}", model_id)
            stored += 1
            continue
        ready.append(document)
    if not ready:
        return stored

    scores = scorer.score(samples).tolist()
    for document, label_quality_score in zip(ready, scores):
        collection.update_one(
            {"_id": document["_id"]},
            {"$set": {"label_quality_score": label_quality_score, "label_quality_model": model_id},
             "$unset": {"label_quality_error": ""}},
        )
    return stored + len(ready)


def run(collection, scorer, fetcher, model_id, batch=None, limit=None, daemon=False, rescore=False):
    """Score queued documents until the queue is empty (or `limit` is reached).

    `batch` images go into one model call (default: the model's, see MODEL.md).

    Results are stored with label_quality_model = `model_id`; `rescore` also
    queues the documents stored with another model_id (see queue()). A round
    in which every document drawn has to wait for its host ends the run too.
    With `daemon`, the queue is polled again every IDLE_SLEEP seconds instead.
    Returns the number of documents stored.
    """
    match = queue(model_id, rescore)
    batch = batch or scorer.model.batch
    processed = 0
    while limit is None or processed < limit:
        size = SAMPLE_SIZE if limit is None else min(SAMPLE_SIZE, limit - processed)
        documents = draw(collection, match, size)
        stored = 0
        for start in range(0, len(documents), batch):
            chunk = documents[start:start + batch]
            stored += process_batch(chunk, collection, scorer, fetcher, model_id)
            log.info("%d documents processed", processed + stored)
            # A user request that arrived during the round starts a new round, so it waits
            # for at most one batch instead of the rest of the round.
            following = documents[start + batch:start + batch + 1]
            if following and "requested_at" not in following[0] and has_requests(collection, match):
                break
        processed += stored
        if not stored:
            if not daemon:
                if documents:
                    log.info("%d queued documents are waiting for their hosts; run again later", len(documents))
                else:
                    log.info("No documents left to score")
                break
            sleep(IDLE_SLEEP)
    return processed


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Score how well the detected boxes fit each queued image.")
    parser.add_argument("--batch", type=positive_int,
                        help="images per model call (default: 32 for the unified and thesis models, 4 for paper)")
    parser.add_argument("--limit", type=positive_int,
                        help="stop after this many documents (default: until the queue is empty)")
    parser.add_argument("--daemon", action="store_true",
                        help=f"poll an empty (or waiting) queue every {IDLE_SLEEP}s instead of exiting")
    parser.add_argument("--rescore", action="store_true",
                        help="also score again the documents whose label_quality_model is not the current "
                             "checkpoint's (default: only documents without a score)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    collection = get_collection()
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    log.info("Using device: %s", device)
    try:
        scorer = labelqa.load_model(device)
        model_id = labelqa.model_id()
    except labelqa.CheckpointError as e:
        raise SystemExit(f"label-quality model: {e}") from e
    run(collection, scorer, get_fetcher(), model_id, batch=args.batch, limit=args.limit,
        daemon=args.daemon, rescore=args.rescore)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
