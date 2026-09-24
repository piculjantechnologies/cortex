# Cortex HTTP API

The Flask app (`gunicorn run:app`) serves every endpoint under `/api/`. The
single-page app calls it on its own origin through the reverse proxy, so in
production nothing but `/api/` has to be forwarded to gunicorn.

- [Conventions](#conventions): request format, cookies, CORS, errors, rate limits
- [Endpoint summary](#endpoint-summary)
- [Accounts and sessions](#accounts-and-sessions)
- [Billing](#billing)
- [Data](#data)
- [Removed endpoints](#removed-endpoints)

The database side of these endpoints is described in
[DATA_MODEL.md](DATA_MODEL.md).

## Conventions

**Requests.** Request bodies are JSON objects sent with
`Content-Type: application/json`. An endpoint that reads a body answers a
missing, malformed or non-object body with a 400, never a 415 or 500.
Endpoints that take no body ignore one.

**Sessions.** A successful login sets two cookies, `session` and
`remember_token`. Both are `HttpOnly`, `SameSite=Lax` and `Secure` (unless the
server runs with `COOKIE_SECURE=0`, for local http development), and both
expire after 30 days. A browser client sends them with
`credentials: 'include'`. The login id inside the cookies contains the user's
current session token: logging out or resetting the password replaces that
token, which ends every session of that user on every device.

**CORS.** Only the origin in `FRONTEND_URL` is allowed, with credentials,
methods `GET`, `POST` and `DELETE`, and the `Content-Type` request header. In
the production layout the SPA and the API share one origin and CORS is not
involved; it matters for local development, where the SPA on port 3000 calls
the API on port 5000.

**Errors.** Every error is JSON except those of the Stripe webhook, which
answers in plain text. The shape depends on where the error comes from:

| Source | Body |
| --- | --- |
| Access and framework errors: 401 from a login-protected endpoint, 403 from the subscription gate, 404, 405, 429, and any unexpected 500 | `{"error": "<HTTP reason>", "message": "<description>"}` |
| Validation errors of `/api/get-labeled-data` | `{"error": "Bad Request", "message": "<field>: <problem>; ...", "fields": {"<field>": "<problem>"}}` |
| Account endpoints (`/api/login`, `/api/register`, ...) | `{"message": "<text for the user>"}` |
| Billing endpoints and `/api/delete-account` | `{"error": "<text for the user>"}` |

An unexpected 500 is logged on the server with its traceback; the response
never contains exception details.

**Rate limits.** Limits are counted per client IP address (taken from
`X-Forwarded-For` when `TRUSTED_PROXY_COUNT` proxies are configured) and, for
forgot-password, also per email address. A request over a limit gets
`429 {"error": "Too Many Requests", "message": "10 per 1 minute"}`.

| Endpoint | Limit |
| --- | --- |
| `POST /api/login`, `POST /api/register`, `POST /api/authorize/google` | 10 per minute per IP |
| `POST /api/forgot-password` | 3 per hour per email address, and 3 per hour per IP |

With the default `RATELIMIT_STORAGE_URI=memory://` each gunicorn worker keeps
its own counters, so the effective limit is the number of workers times the
limit above.

## Endpoint summary

| Method | Path | Access | Purpose |
| --- | --- | --- | --- |
| POST | `/api/register` | public | Create an email and password account |
| POST | `/api/login` | public | Log in with email and password |
| POST | `/api/authorize/google` | public | Log in (or sign up) with a Google ID token |
| POST | `/api/logout` | logged in | Log out everywhere |
| GET | `/api/check-auth` | public | Is this browser logged in? |
| GET | `/api/user` | logged in | The logged-in user's email |
| POST | `/api/forgot-password` | public | Email a password-reset link |
| POST | `/api/reset-password` | public (reset token) | Set a new password |
| DELETE | `/api/delete-account` | logged in | Cancel billing and delete the account |
| POST | `/api/create-checkout-session` | logged in | Start a Stripe Checkout for the subscription |
| GET | `/api/check-subscription-status` | logged in | Does this user have data access? |
| POST | `/api/create-portal-session` | logged in | Open the Stripe billing portal |
| POST | `/api/webhook` | Stripe signature | Receive Stripe events |
| POST | `/api/get-labeled-data` | subscriber | Search the image collection, or export it as CSV |
| POST | `/api/images/lookup` | subscriber | What Cortex knows about up to 50 image URLs (browser extension) |
| POST | `/api/images/analyse` | subscriber | Queue an image URL for detection and scoring (browser extension) |

"Logged in" endpoints answer `401 {"error": "Unauthorized", "message":
"Authentication required."}` without a valid session.

## Accounts and sessions

### POST /api/register

Creates an account with an email address and a password. It does not log the
user in.

Request: `{"email": "user@example.com", "password": "at least 6 characters"}`

The email is trimmed and stored in lower case. It must look like an address
(`name@domain.tld`) and be at most 254 characters long. The password must be
6 to 128 characters long.

| Status | Body |
| --- | --- |
| 201 | `{"message": "User registered successfully"}` |
| 400 | `{"message": "All fields are required"}`, `"Invalid email address."`, `"Password must be at least 6 characters long."`, `"Password must be at most 128 characters long."` or `"User already exists"` (the check ignores case) |
| 409 | `{"message": "User already exists"}`: a concurrent registration with the same email won |
| 429 | rate limit |

### POST /api/login

Request: `{"email": "user@example.com", "password": "..."}`

The email lookup ignores case. Login checks no email format or password
length, so every stored account can log in.

| Status | Body |
| --- | --- |
| 200 | `{"message": "Login successful"}`, with the session cookies |
| 400 | `{"message": "Email and password are required"}` |
| 401 | `{"message": "Invalid credentials"}`: unknown email, wrong password, or an account that only signs in with Google |
| 429 | rate limit |

### POST /api/authorize/google

Logs in with a Google ID token, and creates the account on the first sign-in.
The SPA gets the token from Google Identity Services (the "Sign in with
Google" button) and posts it unchanged.

Request: `{"credential": "<Google ID token (a JWT)>"}`

The server verifies the token's signature against Google's published
certificates (fetched with a 10-second timeout), its expiry and issuer, and
that its audience is `GOOGLE_CLIENT_ID`. The token must carry `sub`, `email`
and `email_verified: true`. A new account stores the Google user id, the email
in lower case and the name; it has no password.

| Status | Body |
| --- | --- |
| 200 | `{"message": "Login successful"}`, with the session cookies |
| 400 | `{"message": "Credential missing"}` |
| 400 | `{"message": "Failed to authenticate"}`: the token has no `sub` or `email` |
| 400 | `{"message": "An account with this email already exists. Please log in using your email and password."}`: the email belongs to an account that is not linked to this Google user |
| 401 | `{"message": "Failed to authenticate"}`: invalid, expired or foreign token, or the email is not verified |
| 429 | rate limit |
| 500 | `{"message": "An error occurred while creating your account."}`: creating the new account collided with an account created at the same moment (for example a registration with the same email), and no account for this Google user exists afterwards |
| 502 | `{"message": "Google sign-in is unavailable, please try again later."}`: Google's certificates could not be fetched |

### POST /api/logout

Logged in. No body. Replaces the user's session token, which ends every
session and remember cookie of this user on every device, then clears the
cookies of this browser.

| Status | Body |
| --- | --- |
| 200 | `{"message": "Logged out successfully"}` |
| 401 | not logged in |

### GET /api/check-auth

The SPA calls this once at start-up to learn whether its cookies still hold a
valid session.

| Status | Body |
| --- | --- |
| 200 | `{"authenticated": true, "user": {"name": "Ada" or null, "email": "user@example.com"}}` |
| 401 | `{"authenticated": false}` |

### GET /api/user

Logged in. Returns `200 {"email": "user@example.com"}`.

### POST /api/forgot-password

Emails a password-reset link through Mailgun.

Request: `{"email": "user@example.com"}`

The answer is the same whether or not an account exists, so the endpoint does
not reveal which addresses are registered. Only accounts that have a password
get an email; Google-only accounts do not. The link is
`<FRONTEND_URL>/reset-password/<token>`. The token is valid for one hour and
works once: it is bound to the current password, so it stops working as soon
as the password changes. A Mailgun failure is logged (without the address) and
the answer is still 200.

| Status | Body |
| --- | --- |
| 200 | `{"message": "If an account exists for that email, a reset link has been sent."}` |
| 400 | `{"message": "A valid email address is required."}` |
| 429 | rate limit (3 per hour per email address and per IP) |

### POST /api/reset-password

Sets a new password with the token from the reset link. The token travels in
the body, never in the URL path.

Request: `{"token": "<token from the link>", "password": "new password"}`

The new password follows the registration rules (6 to 128 characters). A
successful reset also replaces the session token, which logs the user out
everywhere; it does not log the user in.

| Status | Body |
| --- | --- |
| 200 | `{"message": "Password has been reset successfully!"}` |
| 400 | `{"message": "Invalid or expired token."}`: missing, tampered, older than one hour, or already used |
| 400 | `{"message": "Password must be at least 6 characters long."}` or `"... at most 128 characters long."`; the token stays valid for another try |
| 404 | `{"message": "User not found."}`: the account was deleted after the link was sent |

### DELETE /api/delete-account

Logged in. No body. For a user with a Stripe customer, it first cancels every
subscription that is not already `canceled` or `incomplete_expired` and then
deletes the Stripe customer. Only when that succeeds is the account row
deleted and the user logged out. A customer that Stripe no longer knows
counts as already deleted. The SPA has no screen for this endpoint.

| Status | Body |
| --- | --- |
| 200 | `{"message": "Account deleted successfully"}` |
| 401 | not logged in |
| 502 | `{"error": "Could not cancel the subscription; the account was not deleted."}`: a Stripe call failed; the account and the session are kept |

## Billing

Cortex sells one subscription plan (`STRIPE_PRICE_ID`) through Stripe
Checkout. Stripe reports the subscription's state to `/api/webhook`, which
stores it in the user's `subscription_status` column. Access to the data is
decided from that column on every request (see [Data](#data)); the webhook
never has to reach a user's session.

A user has **data access** when `subscription_status` is `active` or
`trialing`, or when the account has complimentary access (the `superuser`
flag, set with `flask --app run make-admin <email>`).

### POST /api/create-checkout-session

Logged in. No body. Returns the URL of a Stripe Checkout page; the SPA
redirects the browser there.

- On the user's first checkout the server creates a Stripe customer and stores
  its id.
- A user who has never had a subscription (`subscription_status` is empty)
  gets a one-day free trial that needs no card up front; if no payment method
  is added by the end of the trial, the subscription is cancelled.
- After payment Stripe sends the browser to
  `STRIPE_SUCCESS_URL?session_id=<id>`, after cancelling to
  `STRIPE_CANCEL_URL`.

| Status | Body |
| --- | --- |
| 200 | `{"url": "https://checkout.stripe.com/..."}` |
| 401 | not logged in |
| 409 | `{"error": "Subscription already active"}`: the stored status is `active` or `trialing`, or Stripe lists such a subscription for the customer; for `past_due` the error is `"Your last payment failed. Update your payment method in the billing portal."` |
| 502 | `{"error": "Could not start checkout"}`: a Stripe call failed |

### GET /api/check-subscription-status

Logged in. Returns `200 {"subscription_active": true, "subscription_status":
"active", "has_billing_account": true}`. `subscription_active` says whether
the user has data access (as defined above); `subscription_status` is the
stored Stripe status (`null` before the first checkout); `has_billing_account`
is true once the user has a Stripe customer, and the SPA then offers the billing
portal even without data access (for example to fix a failed payment). After
returning from Checkout the SPA polls this endpoint until the webhook has
recorded the new subscription.

### POST /api/create-portal-session

Logged in. Any body is ignored: the portal always opens for the logged-in
user's own Stripe customer. Returns the URL of the Stripe billing portal,
where the user can update the payment method or cancel. Leaving the portal
returns to `STRIPE_SUCCESS_URL`.

| Status | Body |
| --- | --- |
| 200 | `{"url": "https://billing.stripe.com/..."}` |
| 401 | not logged in |
| 404 | `{"error": "No billing account found"}`: the user never started a checkout |
| 502 | `{"error": "Could not open the billing portal"}` |

### POST /api/webhook

Called by Stripe, not by the SPA. The raw body must reach the app unmodified,
because its `Stripe-Signature` header is verified with
`STRIPE_WEBHOOK_SECRET`. Subscribe the Stripe webhook endpoint to these
events:

| Event | Subscription it refers to |
| --- | --- |
| `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted` | the event's object |
| `invoice.payment_succeeded`, `invoice.payment_failed` | the invoice's `subscription` (invoices outside a subscription are ignored) |
| `checkout.session.completed` | the session's `subscription`, when `mode` is `subscription` |

For each of these events the server:

1. skips the event if its id is already in the `stripe_event` table (Stripe
   delivers events at least once);
2. retrieves the subscription from Stripe and stores its current `status` as
   `subscription_status` on the user whose `stripe_customer_id` matches the
   subscription's customer. The status is always read from Stripe, never
   derived from the event type, because events can arrive out of order;
3. records the event id in the same database transaction.

Other event types are acknowledged and ignored. Responses are plain text:

| Status | Body | Stripe's reaction |
| --- | --- | --- |
| 200 | `Webhook received` (processed, ignored, or already processed) | done |
| 400 | `Invalid signature` or `Invalid payload` | none (the request is not from Stripe or is malformed) |
| 500 | `Webhook not configured` (empty `STRIPE_WEBHOOK_SECRET`), `Could not retrieve the subscription` or `Database error` | retries later |

## Data

### POST /api/get-labeled-data

Searches the image collection that the offline pipeline fills. The caller
must be logged in (401 otherwise) and have data access (otherwise
`403 {"error": "Forbidden", "message": "An active subscription is
required."}`). The subscription is checked from the database on every call,
so a cancellation recorded by the webhook takes effect on the next request.

The images to return are chosen either with `filter`, a small query
language (see [Filter language](#filter-language) below, used by the web
app), or with `query`, a fixed set of filters. A body may hold one of the two,
not both; with neither, `query`'s defaults apply.

Request (every key is optional; the values shown are the defaults):

```json
{
  "page": 1,
  "per_page": 25,
  "fetch_all": false,
  "sort": "id",
  "query": {
    "include_classes": [],
    "include_mode": "all",
    "exclude_classes": [],
    "min_width": 100,
    "min_height": 0,
    "label_quality_score": 50
  }
}
```

| Key | Rule | Meaning |
| --- | --- | --- |
| `page` | integer >= 1 | page number |
| `per_page` | integer from 1 to 100 | page size |
| `fetch_all` | JSON `true` or `false` | `true` returns a CSV export instead of a page |
| `filter` | a filter-language object | which images match (instead of `query`) |
| `sort` | `"id"`, `"quality"` or `"newest"` | result order: `_id` ascending; highest label-quality score first; `_id` descending (most recently stored first). Ties are broken by `_id`, so pages never overlap |
| `query.include_classes` | list of Pascal VOC class names | the image must contain the listed classes, as `include_mode` says |
| `query.include_mode` | `"all"` or `"any"` | `all`: **every** listed class; `any`: **at least one** of them |
| `query.exclude_classes` | list of Pascal VOC class names | the image must contain **none** of the listed classes |
| `query.min_width`, `query.min_height` | finite number >= 0 | minimum image size in pixels |
| `query.label_quality_score` | finite number from 0 to 100 | minimum label-quality score, in percent (the stored score is from 0 to 1) |

The class names are the 20 Pascal VOC classes: `aeroplane`, `bicycle`,
`bird`, `boat`, `bottle`, `bus`, `car`, `cat`, `chair`, `cow`, `diningtable`,
`dog`, `horse`, `motorbike`, `person`, `pottedplant`, `sheep`, `sofa`,
`train`, `tvmonitor`. Unknown keys are ignored, including `query.source`
(see [Removed request fields](#removed-request-fields)).

Only documents that the pipeline has fully processed can match: images that
failed detection (`error: broken image`, `error: too large`), images not yet
detected or scored, and images whose score is null never appear.

#### Filter language

`filter` is JSON shaped like a MongoDB query, but only the fields and
operators below exist; the backend validates it and builds the database query
itself, so nothing else (`$where`, `$expr`, `$regex`, other fields) is
accepted. `{}` matches every processed image.

| Condition | Matches when |
| --- | --- |
| `{"class": "person"}` | the image has at least one `person` box |
| `{"class": "person", "count": <cmp>}` | ... and the number of `person` boxes compares true (integers >= 1) |
| `{"class": "person", "max_box_area": <cmp>}` | ... and the largest `person` box, as a fraction of the image area (0 to 1), compares true |
| `{"width": <cmp>}`, `{"height": <cmp>}` | image size in pixels (numbers >= 0) |
| `{"label_quality": <cmp>}` | label-quality score (0 to 1) |
| `{"object_count": <cmp>}` | number of boxes of all classes (integers >= 0) |
| `{"collected": <cmp>}` | when the pipeline stored the image: an ISO 8601 date or date-time (`"2026-09-01"`, `"2026-09-01T12:00:00Z"`; UTC unless an offset is given, between 1970 and 2106), to the second; only `$gt`, `$gte`, `$lt`, `$lte` |
| `{"$and": [<condition>, ...]}` | every condition matches |
| `{"$or": [<condition>, ...]}` | at least one condition matches |
| `{"$not": <condition>}` | the condition does not match |

`<cmp>` is a value (equality) or an object of one or more of `$eq`, `$gt`,
`$gte`, `$lt`, `$lte` and `$in` (a list of 1 to 100 values), e.g.
`{"$gte": 640, "$lt": 2000}`. An object with several field keys
(`{"width": {"$gte": 640}, "height": {"$gte": 480}}`) needs all of them; an
object with `$and`, `$or` or `$not` has no other key. A filter nests at most 8
levels and holds at most 64 class and field conditions. "No person" is
`{"$not": {"class": "person"}}`.

Example: two or more people, no dog or car, a person box covering at least
10 % of the image, and a score of at least 0.8:

```json
{"$and": [
  {"class": "person", "count": {"$gte": 2}, "max_box_area": {"$gte": 0.1}},
  {"$not": {"$or": [{"class": "dog"}, {"class": "car"}]}},
  {"label_quality": {"$gte": 0.8}}
]}
```

Class conditions read the per-class counts and box areas the detection stage
stores (see [DATA_MODEL.md](DATA_MODEL.md#image-documents)); on a collection
detected without them, run `python -m pipeline.backfill_object_stats` once. A
problem in a filter is reported under its path, e.g.
`"fields": {"filter.$and[1].count": "must be an integer >= 1"}`.

Every search has a time limit in MongoDB: `QUERY_MAX_TIME_MS` (default
10 s) for a page and its count, `EXPORT_MAX_TIME_MS` (default 5 min) for a
CSV export. A search that runs longer answers 504 and asks the user to narrow
it.

A body that breaks a rule gets a 400 that lists every problem, for example:

```json
{
  "error": "Bad Request",
  "message": "per_page: must be an integer from 1 to 100; include_classes: must be a list of Pascal VOC class names",
  "fields": {
    "per_page": "must be an integer from 1 to 100",
    "include_classes": "must be a list of Pascal VOC class names"
  }
}
```

**Page response** (`fetch_all: false`). Results are in the `sort` order
(by default `_id` ascending, roughly the order in which the pipeline stored
the images), so a query returns the same pages as long as the set of matching documents does
not change. A page past the last one has an empty `output`.

```json
{
  "output": [
    {
      "_id": "PT::66f1c0ffee0123456789abcd",
      "url": "https://example.com/images/cat.jpg",
      "datetime": "2026-09-01 10:15:30.123456",
      "width": 1024,
      "height": 768,
      "hash": "3f0a...64 hex characters",
      "object_detection": {"cat": [[0.12, 0.08, 0.91, 0.97]]},
      "label_quality_score": 0.87,
      "label_quality_model": "738531d09237"
    }
  ],
  "length": 1234,
  "current_page": 1,
  "total_pages": 50,
  "has_next_page": true
}
```

`length` is the number of matching documents. Each entry is the stored
document with its `_id` rendered as `PT::<ObjectId>`; the fields are described
in [DATA_MODEL.md](DATA_MODEL.md#image-documents). `hash` is missing on
documents stored before the pipeline recorded it, `label_quality_model` on
documents scored before the pipeline recorded it.

**CSV export** (`fetch_all: true`). The response is streamed as
`text/csv` with `Content-Disposition: attachment; filename=data.csv`. It holds
every matching row, not just one page, in the `sort` order and at most
`EXPORT_MAX_ROWS` rows (default 100000); `page` and `per_page` are still
validated but otherwise ignored. When the cap is reached the file simply ends,
so compare the row count with `length` from a page request if the result may
be larger. Columns:

| Column | Content |
| --- | --- |
| `_id` | `PT::<ObjectId>` |
| `url` | image URL |
| `width`, `height` | image size in pixels |
| `hash` | SHA-256 of the image bytes (hex); empty for rows stored before the pipeline recorded it |
| `object_detection` | the boxes as JSON, e.g. `{"cat": [[0.12, 0.08, 0.91, 0.97]]}` |
| `label_quality_score` | score from 0 to 1 |

A cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return gets a
leading apostrophe, so spreadsheet programs do not run it as a formula.

| Status | Body |
| --- | --- |
| 200 | page JSON or CSV stream |
| 400 | validation error (see above) |
| 401 | not logged in |
| 403 | no data access |
| 502 | `{"error": "Bad Gateway", "message": "The image database is unavailable."}`; if MongoDB fails in the middle of a CSV stream, the download is aborted |
| 504 | `{"error": "Gateway Timeout", "message": "The search took too long. ..."}`: the search ran past its time limit |

## Browser extension

The [browser extension](../../extension/README.md) calls these two endpoints
from its service worker with the user's session cookie. Both need data access
like `/api/get-labeled-data` (401 without a session, 403 without access) and
answer 502 when MongoDB is unavailable.

Image URLs are matched in the form the pipeline stores them: an absolute
`http://` or `https://` URL of at most 2048 characters, normalised (lower-case
scheme and host, default port removed, unsafe characters percent-encoded).
`data:`, `blob:` and relative URLs cannot be stored.

Each image is described by its **state**:

| `status` | Meaning | Other fields |
| --- | --- | --- |
| `unknown` | not in the collection | none |
| `invalid` | not an http(s) URL Cortex can store (lookup only) | none |
| `queued` | stored, waiting for object detection | `id`, `requested` |
| `scoring` | objects detected, waiting for the label-quality score | `id`, `requested`, `object_detection`, `width`, `height` |
| `done` | processed | as `scoring`, plus `label_quality_score` (0 to 1, or `null` with `label_quality_error`, e.g. `"no detections"`) |
| `failed` | the image could not be processed | `id`, `requested`, `reason` (`broken image` or `too large`) |

`id` is the document's `PT::<ObjectId>`; `requested` is true once someone
asked for the image to be analysed. The fields are described in
[DATA_MODEL.md](DATA_MODEL.md#image-documents).

### POST /api/images/lookup

```json
{"urls": ["https://example.com/photos/cat.jpg", "data:image/png;base64,..."]}
```

`urls` is a list of 1 to 50 strings (otherwise 400 with `fields.urls`). The
answer maps every URL, exactly as sent, to its state:

```json
{"results": {
  "https://example.com/photos/cat.jpg": {
    "id": "PT::66f1c0ffee0123456789abcd", "requested": false, "status": "done",
    "object_detection": {"cat": [[0.12, 0.08, 0.91, 0.97]]}, "width": 1024, "height": 768,
    "label_quality_score": 0.87
  },
  "data:image/png;base64,...": {"status": "invalid"}
}}
```

At most 120 lookups per minute per user; the query has the
`QUERY_MAX_TIME_MS` limit (504 past it).

### POST /api/images/analyse

```json
{"url": "https://example.com/photos/new.jpg"}
```

- A URL Cortex has not stored is inserted into the image collection with
  `source: "extension"` and `requested_at` (the time of the request), so the
  detection stage picks it up: **202** with its state (`queued`).
- A stored image still waiting for detection or scoring gets `requested_at`
  (the first request's time is kept): **202** with its state.
- An image that is already processed (`done` or `failed`) is left alone:
  **200** with its state.

The answer also carries the normalised `url`. The web app never downloads the
image: the pipeline's detection and scoring stages process documents with
`requested_at` before all others, oldest request first (see
[PIPELINE.md](PIPELINE.md#3-detect_objects)), and fetch them with the same
crawl policy as every other image, so an image whose host forbids crawling or
that is not a JPEG, PNG, GIF or WebP ends as `failed`. A URL that is not
http(s) gets 400 with `fields.url`. At most 10 requests per minute and 200 per
day per user (429 beyond).

## Removed endpoints

These paths answer 404:

| Path | Replacement |
| --- | --- |
| `POST /authorize/google` (took a Google access token as `{token}`) | `POST /api/authorize/google` with an ID token as `{credential}` |
| `POST /api/reset-password/<token>` | `POST /api/reset-password` with `{token, password}` |
| `GET /api/get-customer-id` | none; the billing portal uses the logged-in user's customer |
| `GET /api/get-user-info`, `POST /api/set-user-info` | none |
| `/api/admin/...` (admin web interface) | none; `flask --app run make-admin <email>` grants complimentary access |

### Removed request fields

`POST /api/get-labeled-data` no longer handles two things that never had an
effect:

- `query.source` (earlier clients sent `"source": "commoncrawl"`): no query
  ever applied it. A request that still sends it is accepted, and the key is
  ignored like any other unknown key.
- the `error: broken URL` marker in the filter's list of error markers: no
  pipeline stage ever wrote it. The filter now excludes only the markers the
  detection stage writes, `error: broken image` and `error: too large`.
