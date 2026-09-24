# Cortex data pipeline

The pipeline builds the image collection that the web app serves. It runs
offline, separately from the web app, as four Python modules in
`backend/pipeline/`:

```
Common Crawl URL index (data.commoncrawl.org)
  │  1. download_commoncrawl     copy the 300 cdx index files of a crawl
  ▼
Azure Blob Storage (one container per crawl)
  │  2. process_commoncrawl      sample page URLs, fetch the pages, keep JPEG/PNG image URLs
  ▼
MongoDB image collection ◄────── 3. detect_objects          Pascal VOC boxes, box counts and areas, width, height, hash
                         ◄────── 4. estimate_label_quality  label-quality score of the boxes
  │
  ▼
web app: POST /api/get-labeled-data
```

The collection stores image URLs and what the pipeline derived from the
images (boxes, size, hash, score), not the images themselves; the SPA shows
each image from its original URL. The document fields and the queue
conditions between the stages are described in
[DATA_MODEL.md](DATA_MODEL.md#mongodb).

The pipeline does not import Flask or the `app` package; it shares only the
MongoDB collection with the web app.

## Setup

The pipeline has its own requirements (PyTorch, torchvision, OpenCV, Azure
Storage, Beautiful Soup, ...), which the web app does not need. Install them
in a separate virtual environment, from `backend/`:

```sh
cd backend
python3 -m venv .venv-pipeline
. .venv-pipeline/bin/activate
pip install -r pipeline/requirements.txt
```

- Python 3.12 or 3.13.
- The torch wheels from PyPI are the default CUDA build (for torch 2.14 on
  Linux, CUDA 13.0, which needs NVIDIA driver 580 or newer and a GPU of
  compute capability 7.5 (Turing) or newer). For a CPU-only or another CUDA
  build, install `torch==2.14.0` and `torchvision==0.29.0` from the matching
  pytorch.org index first (see the comment in `pipeline/requirements.txt`).
  On an older GPU (Volta or earlier) use the cu126 build or run on the CPU:
  the default build still reports such a GPU as available, and the first
  model call then fails.
- The detection and scoring stages use a CUDA GPU when one is available and
  the CPU otherwise.
- `download_commoncrawl` runs `curl`, which must be on the `PATH`.
- On its first run `detect_objects` downloads torchvision's COCO weights for
  SSDlite320-MobileNetV3-Large into the torch cache.
- On its first run `estimate_label_quality` downloads the selected
  label-quality checkpoint (155 MB for the default unified model, from
  `labelqa.blob.core.windows.net`) into
  `${XDG_CACHE_HOME:-~/.cache}/cortex/labelqa/`. It is not stored in the
  repository. The stage checks its SHA-256 before saving it and again every
  time it loads it, and stops with an error on a mismatch; see
  [MODEL.md](../pipeline/labelqa/MODEL.md#checkpoint-download-and-cache),
  which also covers hosts without internet access.

## Configuration

The pipeline reads its settings from the environment only; unlike the web app
it never loads `.env`, so export the variables in the shell (or the service
definition) that runs a stage.

| Variable | Used by | Required | Default |
| --- | --- | --- | --- |
| `MONGO_URI` | process, detect, estimate | yes; the stage exits with `MONGO_URI is not set` otherwise. The older name `mongo_db_uri` is still read, with a warning. | none |
| `MONGO_DB` | process, detect, estimate | no | `cortex` |
| `MONGO_COLLECTION` | process, detect, estimate | no | `collection` |
| `AZURE_STORAGE_CONNECTION_STRING` | download, process | yes, unless `--connect-str` is given. The older name `connect_str` is still read, with a warning. | none |
| `CC_CONTAINER` | download, process | no; `--container` overrides it | the crawl id in lower case, e.g. `cc-main-2022-40` |
| `LABELQA_MODEL` | estimate | no; the label-quality model: `unified`, `thesis` or `paper` (see [MODEL.md](../pipeline/labelqa/MODEL.md)) | `unified` |
| `LABELQA_CHECKPOINT` | estimate | no; a local checkpoint file of the selected model to load instead of the cache, verified like the cache file (see [MODEL.md](../pipeline/labelqa/MODEL.md#checkpoint-download-and-cache)) | the cache file, downloaded on first use |
| `LABELQA_CHECKPOINT_URL` | estimate | no; where the checkpoint is downloaded from, for example a mirror | the selected model's release URL in MODEL.md |
| `LABELQA_CHECKPOINT_SHA256` | estimate | no; the expected SHA-256, only for another checkpoint of the selected model's network | the selected model's released hash |
| `XDG_CACHE_HOME` | estimate | no; the cache directory is `$XDG_CACHE_HOME/cortex/labelqa` | `~/.cache` |

`MONGO_URI`, `MONGO_DB` and `MONGO_COLLECTION` must point at the same
collection as the web app's settings of the same names. Prefer the environment
variable to `--connect-str`: a command-line argument is visible in the process
list and the shell history.

## Running the stages

Run every stage from `backend/` with the pipeline environment active. Each
stage logs its progress to standard error and exits on its own when its work
is done; `--help` lists the options.

### 1. download_commoncrawl

```sh
python -m pipeline.download_commoncrawl --dest-path "$HOME/cc-index" [--crawl CC-MAIN-2022-40] [--container NAME] [--connect-str ...]
```

Copies the 300 index files `cdx-00000.gz` ... `cdx-00299.gz` of the crawl from
`https://data.commoncrawl.org/cc-index/collections/<crawl>/indexes/` into the
blob container. Each file is downloaded with `curl` into `--dest-path`,
uploaded, and deleted locally after a successful upload. Files already in the
container are skipped, so an interrupted run can simply be started again. A
failed download (including an HTTP error) is not uploaded and its partial file
is removed; the command then exits with status 1 so the run can be repeated.
The container must already exist.

`--crawl` takes a Common Crawl crawl id of the form `CC-MAIN-YYYY-WW`
(default `CC-MAIN-2022-40`).

### 2. process_commoncrawl

```sh
python -m pipeline.process_commoncrawl [--blob-id N] [--crawl CC-MAIN-2022-40] [--container NAME] [--connect-str ...] [--limit 100000]
```

Each round takes one index file that is not yet recorded in the
`processed_blobs` collection, picked at random (or file N with `--blob-id`),
and:

1. streams the file and draws a uniform random sample of at most `--limit`
   page URLs (default 100000); malformed lines are skipped;
2. fetches each page under the [crawl policy](#crawl-policy) (at most 5 MB);
3. resolves the `src` of every `<img>` and `<amp-img>` against the page URL and
   normalises it; non-http(s) URLs and URLs already in the collection are
   skipped;
4. downloads only the first 8 KB of each remaining URL and keeps it if those
   bytes are a JPEG or PNG;
5. inserts `{url, datetime}`. The unique index on `url` makes the insert safe
   when several ingest processes run at once;
6. records the file in `processed_blobs`.

The stage exits once every index file of the container is recorded, or after
the single file given with `--blob-id` (which is processed even if it is
already recorded). On an Azure, MongoDB or OS error it waits and retries,
starting at 30 seconds and doubling up to 30 minutes; with `--blob-id` it exits
with status 1 instead.

### 3. detect_objects

```sh
python -m pipeline.detect_objects [--batch 8] [--limit N] [--daemon]
```

Takes documents without `object_detection`, up to 1000 at a time in random
order, and processes them in batches of `--batch` images per model call:

- downloads the image (at most 20 MB) and decodes it to RGB with its EXIF
  orientation applied; grayscale, palette, RGBA and CMYK images are converted;
- runs torchvision's SSDlite320-MobileNetV3-Large with COCO weights, keeps
  detections with a confidence of at least 0.9, maps the COCO names to the 20
  Pascal VOC classes and drops every other class;
- stores the boxes (normalised `[x1, y1, x2, y2]` per class), the number of
  boxes and the area of the largest box per class (`object_counts`,
  `object_max_area`, `object_total`; the web app's class filters read them),
  `width`, `height` and `hash` (SHA-256 of the image bytes).

An image that cannot be downloaded or decoded gets
`object_detection: "error: broken image"`, one wider or taller than 8192 px
`"error: too large"`.

**Requested images first.** Each round starts with the queued documents that
have `requested_at` (images a user sent from the browser extension, see
[API.md](API.md#browser-extension)), oldest request first, and fills the rest
of the round with a random sample of the others. A request that arrives during
a round starts a new round after the current batch, so with `--daemon` running
a requested image is detected within one batch or one idle poll (at most
60 s). The scoring stage (below) orders its queue the same way.

#### backfill_object_stats

```sh
python -m pipeline.backfill_object_stats [--all] [--batch 1000]
```

Adds `object_counts`, `object_max_area` and `object_total` to detected
documents that do not have them, computed from their stored boxes: no image
is downloaded and no model runs. `--all` recomputes every detected document.
Run it once on a collection detected without these fields; running it again is
harmless.

### 4. estimate_label_quality

```sh
python -m pipeline.estimate_label_quality [--batch N] [--limit N] [--daemon] [--rescore]
```

Takes documents whose `object_detection` holds boxes (or `{}`) and that have
no `label_quality_score`, up to 1000 at a time in random order, downloads each
image again and scores its boxes with the label-quality model in batches of
`--batch` (default: 32 for the unified and thesis models, 4 for paper). The
result, the probability that the boxes are correct, is stored as
`label_quality_score` (0 to 1; the API's filter takes percent), together with
`label_quality_model`, the first 12 hex digits of the checkpoint's SHA-256
(`738531d09237` for the default unified model). A document that cannot be scored gets
`label_quality_score: null` and a `label_quality_error`; so does a document
without boxes of a Pascal VOC class (`{}`), which is not scored. Class names
outside the 20 Pascal VOC classes are ignored.

`LABELQA_MODEL` selects one of the three checkpoints released in the
[label-quality-assurance](https://github.com/piculjantechnologies/label-quality-assurance)
repository: `unified` (the default; `unified/` folder, Pascal VOC), `thesis`
(`thesis/` folder, Pascal VOC) or `paper` (`paper/` folder, COCO). Their
networks and preprocessing are vendored in `pipeline/labelqa/`.
[MODEL.md](../pipeline/labelqa/MODEL.md) describes the three checkpoints, why
unified is the default, how a checkpoint is downloaded and cached, how an
image and its boxes become each model's input, and the published evaluations.

Without `--rescore` only documents without a `label_quality_score` are
scored. `--rescore` also queues every document with an `object_detection`
object whose `label_quality_model` is not the current checkpoint's, including
documents scored before the field was stored, and stores new results for them;
a successful score removes an earlier `label_quality_error`. Documents that
cannot be scored get the current `label_quality_model` as well, so a
`--rescore` run ends like any other. After a switch to another model or
checkpoint, run the stage once with `--rescore`; `--limit` and `--daemon` work
as usual.
The `--rescore` queue cannot use an index: each round of up to 1000 documents
scans the whole collection, so on a large collection a full rescore costs
about one collection scan per 1000 documents.

The stage runs on a CUDA GPU when one is available, otherwise on the CPU.
Measured with 4 CPU threads, a batch takes about 5 s with the unified model
(32 images), 3 s with thesis (32 images) and 5 s with paper (4 images); on a
GPU, PyTorch allocates at most about 1.1 GB, 0.5 GB and 0.9 GB for these
batches. The image downloads, at most one request per second per host, usually
take longer.

### Common options and behaviour

- `--limit N` stops a detection or scoring run after N documents.
- Without `--daemon` a detection or scoring run exits when its queue is
  empty. With `--daemon` it checks the queue again every 60 seconds, so it can
  run next to an ingest process and pick up new documents as they arrive.
- An error in a single document (download, decoding, preprocessing) marks
  that document as described above and the run continues. An error of the
  model call itself, such as running out of GPU memory, stops the run; start
  it again with a smaller `--batch`.
- Existing results are never recomputed, except by the scoring stage's
  `--rescore` (see above). To detect or score documents again, remove the
  fields that take them out of the queue (`object_detection`, `width`,
  `height`, `hash` for detection; `label_quality_score`,
  `label_quality_error` and `label_quality_model` for scoring).
- This applies to documents that the previous release of the pipeline
  processed: they keep boxes and sizes computed without the EXIF orientation
  and the CMYK fix, have no `hash`, and those without detections (`{}`) keep a
  numeric score instead of `null`. The differences and the commands that
  re-queue them are in
  [DATA_MODEL.md](DATA_MODEL.md#documents-from-an-earlier-release).
- Several copies of a detection or scoring stage can run at once, but they may
  draw the same document and repeat its work.

## Crawl policy

Every request that the ingest, detection and scoring stages make to a web
site (pages, images and robots.txt) goes through one HTTP client,
`pipeline/http.py`, which applies these rules:

**Identification.** Requests carry the User-Agent
`CortexBot/1.0 (+https://github.com/piculjantechnologies/cortex)`.

**robots.txt.**
- It is checked before every request, against the CortexBot user agent, and
  cached per origin (scheme, host and port) for 24 hours.
- If robots.txt answers 401, 403, 429 or 5xx, or cannot be fetched at all, the
  whole host is treated as disallowed for one hour. Any other 4xx (such as a
  missing robots.txt) allows everything.
- A `Crawl-delay` is honoured; a host whose `Crawl-delay` is longer than 30
  seconds is skipped.

**Rate.**
- Two requests to the same host are at least one second apart, or the
  robots.txt `Crawl-delay` when that is longer.
- A 429 or 503 answer pauses the host for its `Retry-After` (60 seconds when
  the header is missing, at most one hour). While a host is paused for longer
  than 30 seconds its URLs are skipped rather than waited for.
- The throttle and the robots.txt cache belong to one process: N copies of a
  stage running at once can each reach a host once per second. The same holds
  across stages, since each stage is its own process: ingest, detection and
  scoring running at the same time can together reach one host up to three
  times per second.

**Network safety.**
- Only `http` and `https` URLs are fetched.
- The host must resolve to globally routable addresses only, and the address
  actually connected to is checked again, so no URL can reach loopback,
  private or link-local networks (DNS rebinding included).
- Redirects (at most 5) are followed one hop at a time, and every hop is
  checked like the first URL, robots.txt included.
- Timeouts: 5 seconds to connect and for each read from the connection. A
  body is read in chunks of 64 KB, and after each chunk the read is abandoned
  once it has taken more than 30 seconds in total. The check runs between
  chunks, so a server that keeps sending a few bytes within every 5 seconds
  can hold a request longer than 30 seconds.
- Size caps: pages 5 MB, images 20 MB, robots.txt 500 KB; the ingest stage
  reads only the first 8 KB of an image.
- Proxy settings from the environment and `~/.netrc` are ignored.

A URL that these rules refuse counts as a failed download: the ingest stage
skips the page or image, the detection stage marks the document
`error: broken image`, and the scoring stage stores a null score with
`label_quality_error: "fetch: <reason>"` (for example
`fetch: disallowed by robots.txt`). Such documents are not retried
automatically.

The download stage talks only to Common Crawl's own index host. It uses `curl`
with the same User-Agent and does not consult robots.txt.

## Operating the pipeline host

The pipeline fetches URLs found on arbitrary third-party pages. The HTTP
client ignores `~/.netrc` and `curl` is run without `--netrc`, but keep the
host free of credentials the pipeline does not need anyway: no `~/.netrc`, and
only the MongoDB and Azure Storage credentials in the pipeline's environment.

Use of the Common Crawl index is subject to the Common Crawl Terms of Use, and
the images at the collected URLs belong to their rights holders; see
[NOTICE](../../NOTICE).

## Tests

The pipeline tests are marked `pipeline` and live in `tests/pipeline/`. Run
them in the pipeline environment with the development tools installed:

```sh
cd backend
pip install -r pipeline/requirements.txt -r requirements-dev.txt
pytest -q -m pipeline tests/pipeline
```

They use stubs instead of the network, MongoDB and Azure, and need no GPU. A test module skips
itself when one of its dependencies is missing: in the web app's environment
only the HTTP client tests (`test_pipeline_http.py`) run. The tests that load
a released label-quality checkpoint run only when it is on the machine (its
cache file from an earlier scoring run, or `LABELQA_CHECKPOINT` for the model
`LABELQA_MODEL` selects) and are skipped otherwise; the tests never download
a checkpoint. CI runs the web app
tests only (`pytest -m "not pipeline"`); it lints the pipeline code with
`ruff check .` but does not run the pipeline tests.
