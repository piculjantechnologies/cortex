"""Backfill: store object_stats for documents detected before the stage wrote them.

Reads each detected document's object_detection and sets object_counts,
object_max_area and object_total (see pipeline.object_stats); no image is
downloaded and no model runs. Documents that already have the fields are
skipped unless --all is given. Safe to run again at any time.

    python -m pipeline.backfill_object_stats [--all] [--batch 1000]
"""
import argparse
import logging

from pymongo import UpdateOne

from .config import positive_int
from .db import get_collection
from .object_stats import object_stats

log = logging.getLogger(__name__)

# Detected documents: object_detection is a {class: boxes} map, not an error marker.
DETECTED = {"object_detection": {"$type": "object"}}


def run(collection, batch=1000, everything=False):
    """Set the fields on every matching document; returns the number updated."""
    query = dict(DETECTED) if everything else {**DETECTED, "object_total": {"$exists": False}}
    updated = 0
    operations = []
    for document in collection.find(query, {"object_detection": 1}):
        operations.append(UpdateOne({"_id": document["_id"]}, {"$set": object_stats(document["object_detection"])}))
        if len(operations) == batch:
            updated += collection.bulk_write(operations, ordered=False).modified_count
            operations = []
            log.info("%d documents updated", updated)
    if operations:
        updated += collection.bulk_write(operations, ordered=False).modified_count
    log.info("Done: %d documents updated", updated)
    return updated


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Store per-class box counts and largest box areas for detected documents.")
    parser.add_argument("--all", action="store_true",
                        help="recompute every detected document, not only those without the fields")
    parser.add_argument("--batch", type=positive_int, default=1000,
                        help="documents per bulk write (default: 1000)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run(get_collection(), batch=args.batch, everything=args.all)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
