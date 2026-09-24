# Cortex data model

Cortex keeps its data in two databases:

- **MySQL** holds the user accounts, their subscription state and the Stripe
  events already processed. Only the web app uses it, through SQLAlchemy
  (`app/models.py`).
- **MongoDB** holds the image collection. The offline pipeline writes it and
  the web app only reads it (`POST /api/get-labeled-data`).

The HTTP side is described in [API.md](API.md), the pipeline in
[PIPELINE.md](PIPELINE.md).

## MySQL

The connection comes from `DB_HOST`, `DB_PORT` (default 3306), `DB_NAME`,
`DB_USER`, `DB_PASSWORD` and, for TLS, `DB_SSL_CA` (see
[`../.env.example`](../.env.example)).

### Creating the tables

```sh
cd backend
flask --app run init-db
```

`init-db` creates the tables that do not exist yet. It never alters an
existing table; see [Upgrading an existing database](#upgrading-an-existing-database).
The app itself does not create tables at start-up.

The statements it issues on MySQL are:

```sql
CREATE TABLE user (
    id INTEGER NOT NULL AUTO_INCREMENT,
    superuser BOOL,
    google_id VARCHAR(255),
    email VARCHAR(255) NOT NULL,
    name VARCHAR(255),
    password VARCHAR(255),
    stripe_customer_id VARCHAR(255),
    subscription_status VARCHAR(255),
    session_token VARCHAR(64) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (google_id),
    UNIQUE (email),
    UNIQUE (stripe_customer_id)
);

CREATE TABLE stripe_event (
    id VARCHAR(255) NOT NULL,
    created INTEGER,
    type VARCHAR(255),
    PRIMARY KEY (id)
);
```

### Table `user`

| Column | Meaning |
| --- | --- |
| `id` | primary key |
| `email` | login email, unique. New accounts store it trimmed and in lower case; lookups ignore case. |
| `password` | Werkzeug password hash (new hashes use `pbkdf2:sha256`); `NULL` for accounts that sign in only with Google |
| `google_id` | Google user id (`sub` of the ID token); `NULL` for email and password accounts |
| `name` | display name from Google; `NULL` for email and password accounts |
| `stripe_customer_id` | Stripe customer id, set at the user's first checkout |
| `subscription_status` | status of the user's Stripe subscription as last reported by the webhook (see below); `NULL` until the webhook has recorded a subscription for the user |
| `superuser` | complimentary data access without a subscription. It grants nothing else. Only `flask --app run make-admin <email>` sets it; `NULL` counts as false. |
| `session_token` | random token that is part of the login cookies; replacing it (logout, password reset) ends every session and remember cookie of the user |

`subscription_status` holds Stripe's subscription status string: `trialing`,
`active`, `past_due`, `unpaid`, `canceled`, `incomplete`,
`incomplete_expired` or `paused`. The app uses it in three places:

| Rule | Statuses |
| --- | --- |
| Data access (`/api/get-labeled-data`, `/api/check-subscription-status`) | `active` or `trialing`, or `superuser` set |
| A new checkout is refused with 409 | `active`, `trialing` or `past_due` |
| The first checkout includes the one-day free trial | `NULL` |

Only the webhook writes this column, and access is decided from it on every
request, so a status change applies to the user's next request without
touching their session.

To give an existing account complimentary access:

```sh
flask --app run make-admin user@example.com
```

The email match ignores case; an unknown email exits with an error. There is
no command to remove the flag; clear it in SQL
(`UPDATE user SET superuser = 0 WHERE email = '...';`).

### Table `stripe_event`

One row per Stripe webhook event that changed (or could have changed) a
subscription status. The webhook inserts the row in the same transaction as
the status update and skips events whose id is already present, so a
redelivered event is processed once.

| Column | Meaning |
| --- | --- |
| `id` | Stripe event id (`evt_...`), primary key |
| `created` | the event's `created` Unix timestamp |
| `type` | event type, e.g. `customer.subscription.updated` |

Rows are never deleted by the app. The table grows by a few rows per
subscription per billing period. Processing an event a second time would only
repeat the status lookup at Stripe, so old rows can be pruned at any time.

### Upgrading an existing database

If the `user` table has no `session_token` column (a database created before
session tokens existed, which also lacks the `stripe_event` table), add the
column by hand first: `init-db` adds missing tables but cannot add columns
(MySQL 8):

```sql
ALTER TABLE user ADD COLUMN session_token VARCHAR(64) NULL;
UPDATE user SET session_token = LOWER(HEX(RANDOM_BYTES(32)));
ALTER TABLE user MODIFY session_token VARCHAR(64) NOT NULL;
```

then run `flask --app run init-db`.

Because the login cookie now carries the session token, cookies issued before
the upgrade are no longer accepted and every user has to log in again.

Data access follows `superuser` and `subscription_status` alone, so review the
accounts that have access without a Stripe customer behind it:

```sql
SELECT id, email, superuser, subscription_status FROM user
WHERE superuser = 1 OR (subscription_status IS NOT NULL AND stripe_customer_id IS NULL);
```

Image documents stored by the previous release of the pipeline are described
in [Documents from an earlier release](#documents-from-an-earlier-release).

## MongoDB

The web app and the pipeline connect with `MONGO_URI` and use the database
`MONGO_DB` (default `cortex`) and the collection `MONGO_COLLECTION` (default
`collection`). The pipeline also keeps a `processed_blobs` collection in the
same database.

### Image documents

One document per image URL. Each pipeline stage adds fields:

| Field | Written by | Content |
| --- | --- | --- |
| `_id` | ingest | ObjectId. The API renders it as `PT::<ObjectId>`. |
| `url` | ingest | normalised http(s) URL of a JPEG or PNG image; unique |
| `datetime` | ingest | UTC time of insertion as a string, e.g. `2026-09-01 10:15:30.123456` |
| `source` | web app | `"extension"` on documents inserted by `POST /api/images/analyse`; absent on the crawl's documents |
| `requested_at` | web app | UTC date of the first analysis request (`POST /api/images/analyse`); the detection and scoring stages take documents with it first, oldest first. Absent unless someone asked |
| `object_detection` | detection | the boxes (see below), or the string `error: broken image` (the image could not be downloaded or decoded) or `error: too large` (wider or taller than 8192 px) |
| `width`, `height` | detection | image size in pixels, after applying the EXIF orientation; only on success |
| `object_counts` | detection | number of boxes per class, e.g. `{"person": 2, "dog": 1}`; only classes with boxes; only on success |
| `object_max_area` | detection | area of each class's largest box as a fraction of the image (0 to 1), e.g. `{"person": 0.31}`; only on success |
| `object_total` | detection | number of boxes of all classes; only on success |
| `hash` | detection | SHA-256 of the downloaded image bytes, 64 hex characters; only on success. Documents detected before the pipeline recorded it have no `hash`. |
| `label_quality_score` | scoring | probability from 0 to 1 that the boxes are correct, or `null` when the document could not be scored |
| `label_quality_error` | scoring | why the score is `null`: `no detections` (no boxes of a Pascal VOC class), `fetch: <reason>` or `<exception>: <message>` (at most 300 characters) |
| `label_quality_model` | scoring | the checkpoint the scoring stage used, as the first 12 hex digits of its SHA-256 (`738531d09237` for the default unified model, `27040e830f3f` for thesis, `5727521cb771` for paper; see [MODEL.md](../pipeline/labelqa/MODEL.md)); stored with every score, `null` ones included. Documents scored before the field was stored have none. |

`object_detection` maps Pascal VOC class names to lists of boxes. Each box is
`[x1, y1, x2, y2]`, normalised to 0..1 by the image width and height, with
(0, 0) at the top-left corner. Only detections with a confidence of at least
0.9 are kept. An image without such detections has `{}`, and the scoring
stage stores `null` with `no detections` for it (a document scored by an
earlier release may hold a number instead; see
[Documents from an earlier release](#documents-from-an-earlier-release)).

```json
{
  "_id": {"$oid": "66f1c0ffee0123456789abcd"},
  "url": "https://example.com/images/cat.jpg",
  "datetime": "2026-09-01 10:15:30.123456",
  "object_detection": {"cat": [[0.12, 0.08, 0.91, 0.97]]},
  "object_counts": {"cat": 1},
  "object_max_area": {"cat": 0.713},
  "object_total": 1,
  "width": 1024,
  "height": 768,
  "hash": "<64 hex characters>",
  "label_quality_score": 0.87,
  "label_quality_model": "738531d09237"
}
```

A document moves through three states, and the queries below decide which
stage picks it up:

| State | Condition | Picked up by |
| --- | --- | --- |
| waiting for detection | no `object_detection` field | `detect_objects` |
| waiting for scoring | `object_detection` is an object (boxes or `{}`) and no `label_quality_score` field; with `--rescore` also any `label_quality_model` other than the current checkpoint's, or none | `estimate_label_quality` |
| done | `label_quality_score` is present (a number or `null`) | the API, when the score is a number and the filters match |

With a `query`, the API's filter for `POST /api/get-labeled-data` is:

```js
{
  object_detection: {$nin: ["error: broken image", "error: too large"]},
  // one {"object_detection.<class>": {$exists: true}} per included class, under $and
  // one {"object_detection.<class>": {$exists: true}} per excluded class, under $nor
  width: {$gte: min_width},
  height: {$gte: min_height},
  label_quality_score: {$gte: label_quality_score / 100}
}
```

With a `filter` (the filter language, see [API.md](API.md#filter-language)) it
is `{$and: [{object_detection: {$type: "object"}, label_quality_score: {$type: "number"}}, <translated filter>]}`,
where a class condition becomes `object_counts.<class>` (and
`object_max_area.<class>`) comparisons, `label_quality` becomes
`label_quality_score`, `object_count` becomes `object_total`, `collected`
compares `_id` with the ObjectId of that second, and `$not` becomes `$nor`.

Either is followed by the `sort` stage and either `$skip`/`$limit` (a page) or
`$limit: EXPORT_MAX_ROWS` (the CSV export).

### Documents from an earlier release

The pipeline never re-processes a document that already has results (the
scoring stage's `--rescore` aside), so the documents that the previous release
of the pipeline detected or scored keep what it stored. They differ from what the current stages write:

- **Boxes and size.** They were computed without the EXIF orientation, so a
  photo with an EXIF rotation has its boxes in the unrotated frame (and, when
  turned by 90 degrees, swapped `width` and `height`), and CMYK JPEGs were
  detected on the wrong colour channels. These documents have no `hash`.
- **Error markers.** Grayscale and palette images, and images wider or taller
  than 8192 px, were stored as `error: broken image`.
- **Images without detections.** Documents with `object_detection: {}` got a
  numeric `label_quality_score` instead of `null` with `no detections`. Such a
  document matches the API's filter whenever its score reaches the requested
  minimum, and is then listed and exported without boxes.
- **Box statistics.** Documents without `object_total` have no
  `object_counts` or `object_max_area`, so no class condition of the filter
  language matches them. `python -m pipeline.backfill_object_stats` adds the
  three fields from the stored boxes, without downloading anything.
- **Scores.** Documents without `label_quality_model` were scored with
  another model than the current checkpoint.
  `python -m pipeline.estimate_label_quality --rescore` scores them again,
  together with the documents above that have `{}`.

To bring them in line, remove the fields that take them out of the queues (in
`mongosh`, with your `MONGO_COLLECTION` name) and run the stages again:

```js
const images = db.getCollection("collection");
// Images without detections: the scoring stage then stores null / "no detections".
images.updateMany(
  {object_detection: {}, label_quality_score: {$type: "number"}},
  {$unset: {label_quality_score: ""}}
);
// Everything the previous release detected (boxes but no hash): detect and score again.
images.updateMany(
  {object_detection: {$type: "object"}, hash: {$exists: false}},
  {$unset: {object_detection: "", width: "", height: "", label_quality_score: "", label_quality_error: ""}}
);
// Optionally, retry every image marked broken (this includes the current release's).
images.updateMany(
  {object_detection: "error: broken image"},
  {$unset: {object_detection: ""}}
);
```

Then run `python -m pipeline.detect_objects` and
`python -m pipeline.estimate_label_quality --rescore` (see [PIPELINE.md](PIPELINE.md)).
The first command alone needs no downloads; the second (which also covers the
documents of the first) and the third make the stages download every affected
image again.

### Indexes

The pipeline stages that use MongoDB (ingest, detection and scoring) ensure
these indexes on the image collection when they start; the web app creates
none:

| Index | Purpose |
| --- | --- |
| `url`, unique | makes ingest idempotent: a URL is stored once, even with several ingest processes |
| `label_quality_score`, `width`, `height`, `object_total` | the API's filters |
| `object_counts.$**`, `object_max_area.$**` (wildcard) | class conditions of the filter language |
| `requested_at`, sparse | the stages' queue of requested images |

If the collection already holds duplicate URLs, the unique index cannot be
built and the stage exits with a message; remove the duplicates first.

### `processed_blobs`

The ingest stage records each finished Common Crawl index file here, so it
never picks the same file twice:

| Field | Content |
| --- | --- |
| `container` | Azure Blob Storage container of the index files |
| `blob` | index file name, e.g. `cdx-00042.gz` |
| `crawl` | crawl id, e.g. `CC-MAIN-2022-40` |
| `urls` | number of page URLs sampled from the file |
| `finished_at` | time the file was finished (UTC) |

Delete a record to have the stage process that file again, or run the stage
with `--blob-id N`, which processes file N whether or not it is recorded.
