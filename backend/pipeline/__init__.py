"""Offline data pipeline that fills the MongoDB collection the web app serves.

Stages, each run from backend/ as `python -m pipeline.<module>`:

1. download_commoncrawl: copy the CommonCrawl cdx index files into Azure Blob Storage.
2. process_commoncrawl: sample page URLs from an index file, fetch the pages and
   store the URLs of their JPEG/PNG images.
3. detect_objects: detect Pascal-VOC objects and store boxes, per-class box
   counts and largest box areas, width, height and the sha256 of the image
   bytes. backfill_object_stats adds the counts and areas to documents detected
   without them.
4. estimate_label_quality: score how well the stored boxes fit each image.

The pipeline needs only pymongo from the web stack; it never imports Flask or
the app package. Its dependencies are in pipeline/requirements.txt.
"""
