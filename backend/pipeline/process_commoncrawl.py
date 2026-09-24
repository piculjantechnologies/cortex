"""Ingest stage: store the JPEG/PNG image URLs found on CommonCrawl pages.

Each round takes one cdx index file (cdx-NNNNN.gz) from Azure Blob Storage,
samples up to --limit page URLs from it, fetches those pages and inserts every
new JPEG/PNG <img>/<amp-img> URL into MongoDB. Finished index files are
recorded in the processed_blobs collection; the stage exits once every index
file of the container is recorded, or after the single --blob-id. Index files
missing from the container are skipped, and the stage then exits with status 1.

    python -m pipeline.process_commoncrawl [--blob-id N] [--crawl CC-MAIN-2022-40] [--limit 100000]
"""
import argparse
import gzip
import json
import logging
import random
import time
import zlib
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

import filetype
from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient
from bs4 import BeautifulSoup
from pymongo.errors import DuplicateKeyError, PyMongoError
from url_normalize import url_normalize

from .config import (
    CDX_BLOB_COUNT,
    DEFAULT_CRAWL,
    azure_connection_string,
    cc_container,
    cdx_blob_name,
    crawl_id,
    positive_int,
)
from .db import get_collection, get_database
from .http import MAX_PAGE_BYTES, SNIFF_BYTES, FetchError, get_fetcher

log = logging.getLogger(__name__)

URLS_PER_BLOB = 100_000
BACKOFF_BASE = 30
BACKOFF_MAX = 1800


def upload_func(img_url, collection, fetcher):
    """Insert `img_url` into the collection if it is a new http(s) JPEG or PNG.

    Returns None when the URL was inserted, else {'error': reason}.
    """
    url = url_normalize(img_url)
    if urlsplit(url).scheme not in ("http", "https"):
        return {'error': "URL is not http(s)."}

    # Skip known URLs before downloading anything.
    if collection.find_one({"url": url}, {"_id": 1}):
        return {'error': "URL already exists in the database."}

    # Check the file type from the first bytes only.
    try:
        head = fetcher.fetch(url, max_bytes=SNIFF_BYTES, truncate=True).content
    except FetchError as e:
        return {'error': f"Error retrieving file: {e}"}
    guessed_type = filetype.guess(head)
    if not guessed_type or guessed_type.extension not in ['jpg', 'png']:
        return {'error': "URL does not point to a valid image file."}

    # The unique index on url makes this atomic across concurrent scrapers.
    try:
        collection.insert_one({
            'url': url,
            'datetime': str(datetime.now(timezone.utc).replace(tzinfo=None)),
        })
    except DuplicateKeyError:
        return {'error': "URL already exists in the database."}
    return None


def process_imgtag(imgtag, page_url, collection, fetcher):
    """Resolve the src of an <img> or <amp-img> tag against its page and upload it."""
    src = imgtag.get('src')
    if not src:
        log.debug("image tag without src on %s", page_url)
        return
    upload_func(urljoin(page_url, src.strip()), collection, fetcher)


def _cdx_url(line):
    # cdx line: "<surt> <timestamp> <json>"
    return json.loads(' '.join(line.decode().split()[2:]))['url']


def process_blob_data(fileobj, limit=URLS_PER_BLOB, rng=random):
    """Return a uniform random sample of at most `limit` page URLs from a gzipped cdx stream.

    The stream is read line by line (reservoir sampling), so the index file is
    never held in memory. Malformed lines are skipped; a corrupt or truncated
    gzip stream returns [].
    """
    sample = []
    seen = 0
    try:
        with gzip.GzipFile(fileobj=fileobj) as fin:
            for line in fin:
                try:
                    url = _cdx_url(line)
                except (ValueError, KeyError, TypeError, IndexError):
                    continue
                seen += 1
                if len(sample) < limit:
                    sample.append(url)
                else:
                    j = rng.randrange(seen)
                    if j < limit:
                        sample[j] = url
    except (gzip.BadGzipFile, EOFError, zlib.error) as e:
        log.error("Error reading or parsing blob data: %s", e)
        return []
    rng.shuffle(sample)
    return sample


def scrape_urls(urls, collection, fetcher):
    """Fetch each page and upload the image URLs of its <img> and <amp-img> tags."""
    for i, url in enumerate(urls):
        log.info("Processing URL %d/%d: %s", i + 1, len(urls), url)
        try:
            page = fetcher.fetch(url, max_bytes=MAX_PAGE_BYTES)
            # Bytes, so BeautifulSoup detects the page's encoding.
            imgtags = BeautifulSoup(page.content, 'html.parser').find_all(['img', 'amp-img'])
        except FetchError as e:
            log.info("Skipping page %s: %s", url, e)
            continue
        except Exception:
            log.exception("Skipping page %s", url)
            continue
        for imgtag in imgtags:
            try:
                process_imgtag(imgtag, page.url, collection, fetcher)
            except PyMongoError:
                raise
            except Exception:
                log.exception("Skipping image tag %.200s on %s", imgtag, url)


def next_blob_id(processed, container, rng=random, exclude=()):
    """A random blob id of `container` not yet in processed_blobs (nor in `exclude`), or None."""
    done = set(processed.distinct("blob", {"container": container}))
    remaining = [i for i in range(CDX_BLOB_COUNT) if cdx_blob_name(i) not in done and i not in exclude]
    return rng.choice(remaining) if remaining else None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Sample page URLs from CommonCrawl cdx index files in Azure Blob Storage "
                    "and store the JPEG/PNG image URLs of those pages in MongoDB.")
    parser.add_argument("--blob-id", type=int, metavar="N",
                        help=f"process only cdx-N.gz (0-{CDX_BLOB_COUNT - 1}) and exit")
    parser.add_argument("--crawl", type=crawl_id, default=DEFAULT_CRAWL,
                        help=f"crawl id (default: {DEFAULT_CRAWL})")
    parser.add_argument("--container",
                        help="blob container (default: $CC_CONTAINER, else the crawl id in lower case)")
    parser.add_argument("--connect-str",
                        help="Azure Storage connection string (default: $AZURE_STORAGE_CONNECTION_STRING)")
    parser.add_argument("--limit", type=positive_int, default=URLS_PER_BLOB,
                        help=f"page URLs sampled per index file (default: {URLS_PER_BLOB})")
    args = parser.parse_args(argv)
    if args.blob_id is not None and not 0 <= args.blob_id < CDX_BLOB_COUNT:
        parser.error(f"--blob-id must be between 0 and {CDX_BLOB_COUNT - 1}")
    return args


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    container = cc_container(args.crawl, args.container)
    blob_service_client = BlobServiceClient.from_connection_string(
        azure_connection_string(args.connect_str))
    collection = get_collection()
    processed = get_database()["processed_blobs"]
    fetcher = get_fetcher()

    failures = 0
    missing = set()
    while True:
        blob_id = args.blob_id if args.blob_id is not None else next_blob_id(processed, container, exclude=missing)
        if blob_id is None:
            if missing:
                log.error("%d index files were not found in %s; copy them with download_commoncrawl "
                          "and run this stage again", len(missing), container)
                return 1
            log.info("Every index file in %s is processed", container)
            return 0
        blob = cdx_blob_name(blob_id)
        log.info("Processing blob %s/%s", container, blob)
        try:
            downloader = blob_service_client.get_blob_client(container=container, blob=blob).download_blob()
            urls = process_blob_data(downloader, limit=args.limit)
            if urls:
                scrape_urls(urls, collection, fetcher)
            else:
                log.warning("No valid URLs found in %s", blob)
            processed.update_one(
                {"container": container, "blob": blob},
                {"$set": {"crawl": args.crawl, "urls": len(urls),
                          "finished_at": datetime.now(timezone.utc)}},
                upsert=True)
            failures = 0
        except (AzureError, PyMongoError, OSError) as e:
            if args.blob_id is not None:
                log.error("Error during blob processing: %s", e)
                return 1
            if isinstance(e, ResourceNotFoundError):
                log.error("Skipping %s: not found in %s", blob, container)
                missing.add(blob_id)
                continue
            failures += 1
            delay = min(BACKOFF_MAX, BACKOFF_BASE * 2 ** (failures - 1))
            log.error("Error during blob processing: %s; retrying in %ds", e, delay)
            time.sleep(delay)
            continue
        if args.blob_id is not None:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
