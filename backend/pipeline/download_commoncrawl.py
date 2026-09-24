"""Download stage: copy a crawl's cdx index files from CommonCrawl into Azure Blob Storage.

Index files already in the container are skipped, so an interrupted run can
simply be restarted. The container must exist; the stage stops at the first
upload that finds it missing.

    python -m pipeline.download_commoncrawl --dest-path /path/to/tmp [--crawl CC-MAIN-2022-40]
"""
import argparse
import logging
import os
import subprocess

from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient

from .config import CDX_BLOB_COUNT, DEFAULT_CRAWL, azure_connection_string, cc_container, cdx_blob_name, crawl_id
from .http import USER_AGENT

log = logging.getLogger(__name__)

INDEX_URL = "https://data.commoncrawl.org/cc-index/collections/{crawl}/indexes/{name}"


def download_file(url, local_file_path):
    """Download a file using curl; an HTTP error or a partial download returns False."""
    log.info("Downloading %s...", url)
    try:
        subprocess.run(["curl", "-fsSL", "-A", USER_AGENT, "-o", local_file_path, url], check=True)
    except (subprocess.CalledProcessError, OSError) as e:
        log.error("Failed to download %s: %s", url, e)
        if os.path.exists(local_file_path):
            os.remove(local_file_path)
        return False
    log.info("Downloaded %s", local_file_path)
    return True


def upload_to_azure(local_file_path, container_name, blob_service_client):
    """Upload a file to Azure Blob Storage; a missing container raises ResourceNotFoundError."""
    blob_client = blob_service_client.get_blob_client(container=container_name,
                                                      blob=os.path.basename(local_file_path))
    try:
        with open(local_file_path, "rb") as file:
            blob_client.upload_blob(file, overwrite=True)
        log.info("Uploaded %s to Azure Blob Storage.", os.path.basename(local_file_path))
        return True
    except ResourceNotFoundError:
        raise
    except (AzureError, OSError) as e:
        log.error("Error uploading %s: %s", os.path.basename(local_file_path), e)
        return False


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Download files from Common Crawl (using curl) and upload to Azure Blob Storage.")
    parser.add_argument("--dest-path", required=True,
                        help="Directory where files will be temporarily stored.")
    parser.add_argument("--crawl", type=crawl_id, default=DEFAULT_CRAWL,
                        help=f"Common Crawl crawl id (default: {DEFAULT_CRAWL}).")
    parser.add_argument("--container",
                        help="Azure Blob Storage container name "
                             "(default: $CC_CONTAINER, else the crawl id in lower case).")
    parser.add_argument("--connect-str",
                        help="Azure Blob Storage connection string "
                             "(default: $AZURE_STORAGE_CONNECTION_STRING).")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    container = cc_container(args.crawl, args.container)

    # Ensure the destination path exists
    os.makedirs(args.dest_path, exist_ok=True)

    # Set up Azure Blob Storage client
    blob_service_client = BlobServiceClient.from_connection_string(
        azure_connection_string(args.connect_str))

    failed = 0
    for i in range(CDX_BLOB_COUNT):
        file_name = cdx_blob_name(i)
        if blob_service_client.get_blob_client(container=container, blob=file_name).exists():
            log.info("Skipping %s: already in %s", file_name, container)
            continue
        local_file_path = os.path.join(args.dest_path, file_name)
        file_url = INDEX_URL.format(crawl=args.crawl, name=file_name)

        # Step 1: Download file using curl
        if not download_file(file_url, local_file_path):
            failed += 1
            continue  # Skip to next file if download fails

        # Step 2: Upload to Azure Blob Storage. The local copy goes either way:
        # a rerun downloads every file that is not in the container.
        try:
            uploaded = upload_to_azure(local_file_path, container, blob_service_client)
        except ResourceNotFoundError as e:
            log.error("Cannot upload to container %s (%s); create it and run again", container, e)
            return 1
        finally:
            os.remove(local_file_path)
            log.info("Deleted local file: %s", local_file_path)
        if not uploaded:
            failed += 1

    if failed:
        log.error("%d of %d index files were not copied; run again to retry", failed, CDX_BLOB_COUNT)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
