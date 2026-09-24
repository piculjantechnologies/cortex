"""Environment settings shared by the pipeline stages."""
import logging
import os
import re

log = logging.getLogger(__name__)

DEFAULT_CRAWL = "CC-MAIN-2022-40"
CDX_BLOB_COUNT = 300  # cdx-00000.gz .. cdx-00299.gz per crawl
_CRAWL_ID = re.compile(r"^CC-MAIN-\d{4}-\d{2}$")


def require_env(name, legacy=None):
    """Return $name (or the deprecated $legacy); exit with a message when neither is set."""
    value = os.environ.get(name)
    if not value and legacy and os.environ.get(legacy):
        log.warning("%s is deprecated; set %s instead", legacy, name)
        value = os.environ[legacy]
    if not value:
        raise SystemExit(f"{name} is not set")
    return value


def azure_connection_string(override=None):
    """The Azure Storage connection string: `override`, else the environment."""
    return override or require_env("AZURE_STORAGE_CONNECTION_STRING", legacy="connect_str")


def crawl_id(value):
    """argparse type for --crawl: a CommonCrawl crawl id such as CC-MAIN-2022-40."""
    if not _CRAWL_ID.match(value):
        raise ValueError(value)
    return value


def positive_int(value):
    """argparse type for counts that must be at least 1."""
    number = int(value)
    if number < 1:
        raise ValueError(value)
    return number


def cc_container(crawl=DEFAULT_CRAWL, override=None):
    """The blob container holding a crawl's cdx files.

    `override`, else $CC_CONTAINER, else the crawl id in lower case
    ('cc-main-2022-40' for the default crawl).
    """
    return override or os.environ.get("CC_CONTAINER") or crawl.lower()


def cdx_blob_name(blob_id):
    return f"cdx-{blob_id:05d}.gz"
