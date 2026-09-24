"""Download and ingest stages, shared config and the Mongo helper (no torch needed)."""
import gzip
import io
import json
import random
import subprocess

import pytest

pytest.importorskip("bs4")
pytest.importorskip("azure.storage.blob")
pytest.importorskip("url_normalize")
pytest.importorskip("filetype")
mongomock = pytest.importorskip("mongomock")

from pymongo.errors import ServerSelectionTimeoutError  # noqa: E402

from pipeline import config, db, download_commoncrawl, process_commoncrawl  # noqa: E402
from pipeline.http import FetchError  # noqa: E402

pytestmark = pytest.mark.pipeline

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
HTML = b"<!doctype html><html></html>"


@pytest.fixture
def collection():
    coll = mongomock.MongoClient().cortex.collection
    db.ensure_indexes(coll)
    return coll


def cdx_gz(urls):
    lines = [f"com,example)/{i} 20220930000000 {json.dumps({'url': u, 'status': '200'})}\n"
             for i, u in enumerate(urls)]
    return gzip.compress("".join(lines).encode())


# process_blob_data

def test_process_blob_data_returns_the_page_urls():
    data = cdx_gz(["https://a.example/1", "https://b.example/2"])
    assert sorted(process_commoncrawl.process_blob_data(io.BytesIO(data))) == [
        "https://a.example/1", "https://b.example/2"]


class ReadOnlyStream:
    """Like azure's StorageStreamDownloader: read(size) and a name, no seek."""

    def __init__(self, data, name="cdx-00000.gz"):
        self._buffer = io.BytesIO(data)
        self.name = name

    def read(self, size=-1):
        return self._buffer.read(size)


def test_process_blob_data_reads_a_non_seekable_stream():
    urls = [f"https://site.example/{i}" for i in range(3000)]
    assert sorted(process_commoncrawl.process_blob_data(ReadOnlyStream(cdx_gz(urls)))) == sorted(urls)


@pytest.mark.parametrize("data", [b"not a gzip stream", cdx_gz(["https://a.example/"] * 50)[:-12]],
                         ids=["not-gzip", "truncated"])
def test_process_blob_data_corrupt_gzip_returns_empty_list(data):
    assert process_commoncrawl.process_blob_data(io.BytesIO(data)) == []


def test_process_blob_data_skips_malformed_lines():
    data = gzip.compress(b"garbage\ncom,example)/ 2022 {\"url\": \"https://a.example/\"}\ncom,x)/ 2022 {bad json\n")
    assert process_commoncrawl.process_blob_data(io.BytesIO(data)) == ["https://a.example/"]


def test_process_blob_data_samples_at_most_limit_urls():
    urls = [f"https://site.example/{i}" for i in range(500)]
    sample = process_commoncrawl.process_blob_data(io.BytesIO(cdx_gz(urls)), limit=50, rng=random.Random(1))
    assert len(sample) == 50
    assert len(set(sample)) == 50
    assert set(sample) <= set(urls)
    # not just the first 50 lines of the file
    assert any(int(u.rsplit("/", 1)[1]) >= 50 for u in sample)


# upload_func

def test_upload_func_inserts_png_once(collection, stub_fetcher):
    fetcher = stub_fetcher({"https://img.example/a.png": PNG})
    assert process_commoncrawl.upload_func("https://img.example/a.png", collection, fetcher) is None
    assert process_commoncrawl.upload_func("https://img.example/a.png", collection, fetcher) == {
        "error": "URL already exists in the database."}
    docs = list(collection.find())
    assert len(docs) == 1
    assert docs[0]["url"] == "https://img.example/a.png"
    assert isinstance(docs[0]["datetime"], str)
    assert fetcher.calls == ["https://img.example/a.png"]  # the repeat was not downloaded again


def test_upload_func_sniffs_only_the_first_bytes(collection, stub_fetcher):
    fetcher = stub_fetcher({"https://img.example/a.jpg": JPEG + b"\x00" * 100_000})
    assert process_commoncrawl.upload_func("https://img.example/a.jpg", collection, fetcher) is None
    assert collection.count_documents({}) == 1


def test_upload_func_duplicate_insert_race_is_skipped_by_unique_index(collection, stub_fetcher):
    url = "https://img.example/a.png"
    fetcher = stub_fetcher({url: PNG})
    real_fetch = fetcher.fetch

    def fetch_while_another_scraper_inserts(*args, **kwargs):
        collection.insert_one({"url": url})
        return real_fetch(*args, **kwargs)

    fetcher.fetch = fetch_while_another_scraper_inserts
    assert process_commoncrawl.upload_func(url, collection, fetcher) == {
        "error": "URL already exists in the database."}
    assert collection.count_documents({"url": url}) == 1


@pytest.mark.parametrize("body", [GIF, HTML, b""])
def test_upload_func_rejects_other_file_types(collection, stub_fetcher, body):
    fetcher = stub_fetcher({"https://img.example/x": body})
    result = process_commoncrawl.upload_func("https://img.example/x", collection, fetcher)
    assert result == {"error": "URL does not point to a valid image file."}
    assert collection.count_documents({}) == 0


def test_upload_func_fetch_failure_inserts_nothing(collection, stub_fetcher):
    fetcher = stub_fetcher({"https://img.example/a.png": FetchError("disallowed by robots.txt")})
    result = process_commoncrawl.upload_func("https://img.example/a.png", collection, fetcher)
    assert "robots.txt" in result["error"]
    assert collection.count_documents({}) == 0


# process_imgtag / scrape_urls

def test_relative_src_is_resolved_against_the_page(collection, stub_fetcher):
    from bs4 import BeautifulSoup

    fetcher = stub_fetcher({"https://site.example/img/a.png": PNG})
    tag = BeautifulSoup('<img src="../img/a.png">', "html.parser").img
    process_commoncrawl.process_imgtag(tag, "https://site.example/dir/page.html", collection, fetcher)
    assert collection.find_one()["url"] == "https://site.example/img/a.png"


def test_img_without_src_is_ignored(collection, stub_fetcher):
    from bs4 import BeautifulSoup

    fetcher = stub_fetcher({})
    tag = BeautifulSoup('<img alt="x">', "html.parser").img
    process_commoncrawl.process_imgtag(tag, "https://site.example/", collection, fetcher)
    assert fetcher.calls == []


def test_malformed_src_is_logged_and_the_loop_continues(collection, stub_fetcher, caplog):
    page = (b'<html><body><img src="//exa..mple.com/a.jpg">'
            b'<amp-img src="/b.png"></amp-img></body></html>')
    fetcher = stub_fetcher({
        "https://site.example/page": page,
        "https://site.example/b.png": PNG,
    })
    process_commoncrawl.scrape_urls(["https://site.example/page"], collection, fetcher)
    assert [d["url"] for d in collection.find()] == ["https://site.example/b.png"]
    assert "Skipping image tag" in caplog.text


def test_page_is_parsed_from_bytes_in_its_own_encoding(collection, stub_fetcher):
    page = '<meta charset="iso-8859-2"><img src="/slika-č.png">'.encode("iso-8859-2")
    fetcher = stub_fetcher({
        "https://site.example/p": page,
        "https://site.example/slika-%C4%8D.png": PNG,
    })
    process_commoncrawl.scrape_urls(["https://site.example/p"], collection, fetcher)
    assert collection.count_documents({}) == 1


def test_failed_page_fetch_is_skipped(collection, stub_fetcher):
    fetcher = stub_fetcher({"https://ok.example/": b'<img src="a.png">', "https://ok.example/a.png": PNG})
    process_commoncrawl.scrape_urls(["https://down.example/", "https://ok.example/"], collection, fetcher)
    assert collection.count_documents({}) == 1


def test_unexpected_page_error_is_logged_and_the_loop_continues(collection, stub_fetcher, caplog):
    fetcher = stub_fetcher({
        "https://bad.example/": ValueError("Invalid IPv6 URL"),
        "https://ok.example/": b'<img src="a.png">',
        "https://ok.example/a.png": PNG,
    })
    process_commoncrawl.scrape_urls(["https://bad.example/", "https://ok.example/"], collection, fetcher)
    assert [d["url"] for d in collection.find()] == ["https://ok.example/a.png"]
    assert "Skipping page https://bad.example/" in caplog.text


def test_mongo_errors_are_not_swallowed(stub_fetcher):
    class DownCollection:
        def find_one(self, *args, **kwargs):
            raise ServerSelectionTimeoutError("down")

    fetcher = stub_fetcher({"https://ok.example/": b'<img src="a.png">'})
    with pytest.raises(ServerSelectionTimeoutError):
        process_commoncrawl.scrape_urls(["https://ok.example/"], DownCollection(), fetcher)


# blob bookkeeping

def test_next_blob_id_skips_processed_blobs_and_ends():
    processed = mongomock.MongoClient().cortex.processed_blobs
    processed.insert_many([{"container": "cc-main-2022-40", "blob": config.cdx_blob_name(i)}
                           for i in range(config.CDX_BLOB_COUNT) if i != 42])
    processed.insert_one({"container": "other", "blob": config.cdx_blob_name(42)})
    assert process_commoncrawl.next_blob_id(processed, "cc-main-2022-40") == 42
    processed.insert_one({"container": "cc-main-2022-40", "blob": config.cdx_blob_name(42)})
    assert process_commoncrawl.next_blob_id(processed, "cc-main-2022-40") is None


class FakeCdxService:
    def __init__(self, blobs):
        self.blobs = blobs
        self.requested = []

    def get_blob_client(self, container, blob):
        self.requested.append((container, blob))
        service = self

        class Client:
            def download_blob(self):
                data = service.blobs[blob]
                if isinstance(data, Exception):
                    raise data
                return ReadOnlyStream(data, blob)

        return Client()


@pytest.fixture
def ingest_env(monkeypatch, collection):
    """Wire process_commoncrawl.main to mongomock and a fake blob service; returns a setup function."""
    database = mongomock.MongoClient().cortex

    def setup(blobs, fetcher):
        service = FakeCdxService(blobs)
        monkeypatch.setattr(process_commoncrawl.BlobServiceClient, "from_connection_string",
                            staticmethod(lambda conn: service))
        monkeypatch.setattr(process_commoncrawl, "get_collection", lambda: collection)
        monkeypatch.setattr(process_commoncrawl, "get_database", lambda: database)
        monkeypatch.setattr(process_commoncrawl, "get_fetcher", lambda: fetcher)
        monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true")
        monkeypatch.delenv("CC_CONTAINER", raising=False)
        return service, database.processed_blobs

    return setup


def test_process_main_single_blob_records_it(ingest_env, collection, stub_fetcher):
    fetcher = stub_fetcher({"https://site.example/page": b'<img src="a.png">', "https://site.example/a.png": PNG})
    service, processed = ingest_env({"cdx-00007.gz": cdx_gz(["https://site.example/page"])}, fetcher)
    assert process_commoncrawl.main(["--blob-id", "7"]) == 0
    assert service.requested == [("cc-main-2022-40", "cdx-00007.gz")]
    assert [d["url"] for d in collection.find()] == ["https://site.example/a.png"]
    record = processed.find_one()
    assert (record["container"], record["blob"], record["urls"]) == ("cc-main-2022-40", "cdx-00007.gz", 1)


def test_process_main_single_blob_failure_exits_non_zero(ingest_env, stub_fetcher):
    from azure.core.exceptions import ResourceNotFoundError

    _, processed = ingest_env({"cdx-00007.gz": ResourceNotFoundError("ContainerNotFound")}, stub_fetcher({}))
    assert process_commoncrawl.main(["--blob-id", "7"]) == 1
    assert processed.count_documents({}) == 0


def test_process_main_skips_missing_blobs_and_exits_non_zero(ingest_env, collection, stub_fetcher, monkeypatch):
    from azure.core.exceptions import ResourceNotFoundError

    monkeypatch.setattr(process_commoncrawl.time, "sleep", lambda s: pytest.fail("backed off on a missing blob"))
    fetcher = stub_fetcher({"https://site.example/page": b'<img src="a.png">', "https://site.example/a.png": PNG})
    service, processed = ingest_env({
        "cdx-00005.gz": ResourceNotFoundError("BlobNotFound"),
        "cdx-00006.gz": cdx_gz(["https://site.example/page"]),
    }, fetcher)
    processed.insert_many([{"container": "cc-main-2022-40", "blob": config.cdx_blob_name(i)}
                           for i in range(config.CDX_BLOB_COUNT) if i not in (5, 6)])
    assert process_commoncrawl.main([]) == 1
    assert sorted(service.requested) == [("cc-main-2022-40", "cdx-00005.gz"), ("cc-main-2022-40", "cdx-00006.gz")]
    assert processed.find_one({"blob": "cdx-00006.gz"})["urls"] == 1
    assert processed.find_one({"blob": "cdx-00005.gz"}) is None  # a later run tries it again
    assert collection.count_documents({}) == 1


def test_process_main_exits_when_every_blob_is_processed(ingest_env, stub_fetcher):
    service, processed = ingest_env({}, stub_fetcher({}))
    processed.insert_many([{"container": "cc-main-2022-40", "blob": config.cdx_blob_name(i)}
                           for i in range(config.CDX_BLOB_COUNT)])
    assert process_commoncrawl.main([]) == 0
    assert service.requested == []


@pytest.mark.parametrize("argv", [["--blob-id", "300"], ["--blob-id", "-1"], ["--limit", "0"], ["--crawl", "x; rm"]])
def test_process_rejects_bad_arguments(argv):
    with pytest.raises(SystemExit):
        process_commoncrawl.parse_args(argv)


# download_commoncrawl

class FakeBlobClient:
    def __init__(self, service, name):
        self.service, self.name = service, name

    def exists(self):
        return self.name in self.service.existing

    def upload_blob(self, file, overwrite=False):
        self.service.attempted.append(self.name)
        if self.service.upload_error:
            raise self.service.upload_error
        self.service.uploaded.append(self.name)


class FakeBlobService:
    def __init__(self, existing=(), upload_error=None):
        self.existing = set(existing)
        self.upload_error = upload_error
        self.attempted = []
        self.uploaded = []
        self.containers = set()

    def get_blob_client(self, container, blob):
        self.containers.add(container)
        return FakeBlobClient(self, blob)


def run_download(monkeypatch, tmp_path, service, curl):
    monkeypatch.setattr(download_commoncrawl.BlobServiceClient, "from_connection_string",
                        staticmethod(lambda conn: service))
    monkeypatch.setattr(download_commoncrawl.subprocess, "run", curl)
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true")
    monkeypatch.delenv("CC_CONTAINER", raising=False)
    return download_commoncrawl.main(["--dest-path", str(tmp_path)])


def test_failing_curl_download_is_not_uploaded(monkeypatch, tmp_path):
    service = FakeBlobService()

    def curl(cmd, check):
        open(cmd[cmd.index("-o") + 1], "wb").write(b"partial")
        raise subprocess.CalledProcessError(22, cmd)

    assert run_download(monkeypatch, tmp_path, service, curl) == 1
    assert service.uploaded == []
    assert list(tmp_path.iterdir()) == []  # partial files are removed


def test_download_uses_curl_fail_flag_skips_existing_and_defaults_container(monkeypatch, tmp_path):
    service = FakeBlobService(existing={config.cdx_blob_name(i) for i in range(1, config.CDX_BLOB_COUNT)})
    commands = []

    def curl(cmd, check):
        commands.append(cmd)
        assert check is True
        open(cmd[cmd.index("-o") + 1], "wb").write(b"gz")

    assert run_download(monkeypatch, tmp_path, service, curl) == 0
    assert len(commands) == 1
    assert commands[0][:2] == ["curl", "-fsSL"]
    assert commands[0][-1] == ("https://data.commoncrawl.org/cc-index/collections/"
                               "CC-MAIN-2022-40/indexes/cdx-00000.gz")
    assert service.uploaded == ["cdx-00000.gz"]
    assert service.containers == {"cc-main-2022-40"}
    assert list(tmp_path.iterdir()) == []


def fake_curl(cmd, check):
    open(cmd[cmd.index("-o") + 1], "wb").write(b"gz")


def test_failed_upload_removes_the_local_file(monkeypatch, tmp_path):
    from azure.core.exceptions import ServiceRequestError

    existing = {config.cdx_blob_name(i) for i in range(2, config.CDX_BLOB_COUNT)}
    service = FakeBlobService(existing=existing, upload_error=ServiceRequestError("connection reset"))
    assert run_download(monkeypatch, tmp_path, service, fake_curl) == 1
    assert service.attempted == ["cdx-00000.gz", "cdx-00001.gz"]  # the loop went on
    assert list(tmp_path.iterdir()) == []


def test_missing_container_stops_the_download(monkeypatch, tmp_path):
    from azure.core.exceptions import ResourceNotFoundError

    service = FakeBlobService(upload_error=ResourceNotFoundError("ContainerNotFound"))
    assert run_download(monkeypatch, tmp_path, service, fake_curl) == 1
    assert service.attempted == ["cdx-00000.gz"]
    assert list(tmp_path.iterdir()) == []


# config / db

def test_azure_connection_string_sources(monkeypatch):
    monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
    monkeypatch.delenv("connect_str", raising=False)
    with pytest.raises(SystemExit, match="AZURE_STORAGE_CONNECTION_STRING"):
        config.azure_connection_string()
    monkeypatch.setenv("connect_str", "legacy")
    assert config.azure_connection_string() == "legacy"
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "new")
    assert config.azure_connection_string() == "new"
    assert config.azure_connection_string("override") == "override"


def test_cc_container_defaults(monkeypatch):
    monkeypatch.delenv("CC_CONTAINER", raising=False)
    assert config.cc_container() == "cc-main-2022-40"
    assert config.cc_container("CC-MAIN-2023-50") == "cc-main-2023-50"
    monkeypatch.setenv("CC_CONTAINER", "mine")
    assert config.cc_container() == "mine"
    assert config.cc_container(override="cli") == "cli"


def test_get_collection_uses_env_and_creates_indexes(monkeypatch):
    created = []

    def fake_client(uri):
        created.append(uri)
        return mongomock.MongoClient()

    monkeypatch.setattr(db, "_client", None)
    monkeypatch.setattr(db.pymongo, "MongoClient", fake_client)
    monkeypatch.delenv("MONGO_URI", raising=False)
    monkeypatch.setenv("mongo_db_uri", "mongodb://legacy")
    monkeypatch.setenv("MONGO_DB", "testdb")
    monkeypatch.setenv("MONGO_COLLECTION", "images")
    coll = db.get_collection()
    assert db.get_collection().full_name == coll.full_name == "testdb.images"
    assert created == ["mongodb://legacy"]  # one client per process
    indexes = coll.index_information()
    assert indexes["url_1"]["unique"] is True
    assert {"label_quality_score_1", "width_1", "height_1", "object_total_1"} <= set(indexes)
    assert {"object_counts.$**_1", "object_max_area.$**_1"} <= set(indexes)


def test_get_client_fails_fast_without_uri(monkeypatch):
    monkeypatch.setattr(db, "_client", None)
    monkeypatch.delenv("MONGO_URI", raising=False)
    monkeypatch.delenv("mongo_db_uri", raising=False)
    with pytest.raises(SystemExit, match="MONGO_URI"):
        db.get_client()


def test_duplicate_urls_block_the_unique_index_with_a_clear_message():
    coll = mongomock.MongoClient().cortex.collection
    coll.insert_many([{"url": "u"}, {"url": "u"}])
    with pytest.raises(SystemExit, match="duplicate urls"):
        db.ensure_indexes(coll)
