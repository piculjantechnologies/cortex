# Cortex backend

The backend has two independent parts that share one MongoDB collection:

- **The web app**, a Flask API (`gunicorn run:app`). It handles accounts
  (email and password, or Google), Stripe subscriptions and password-reset
  emails, and serves the image collection to subscribers as paged JSON or a
  CSV export. Accounts live in MySQL, images in MongoDB.
- **The offline pipeline** (`python -m pipeline.<stage>`). It collects image
  URLs from pages listed in the Common Crawl index, detects Pascal VOC objects
  and scores the quality of the boxes. See [docs/PIPELINE.md](docs/PIPELINE.md).

Reference documentation:

- [docs/API.md](docs/API.md): every endpoint, request, response and error
- [docs/DATA_MODEL.md](docs/DATA_MODEL.md): MySQL tables, MongoDB documents,
  upgrading an existing database
- [docs/PIPELINE.md](docs/PIPELINE.md): pipeline stages, crawl policy, operation
- [pipeline/labelqa/MODEL.md](pipeline/labelqa/MODEL.md): the three
  label-quality models (unified, the default; thesis; paper) and their
  checkpoints

## Layout

```
backend/
├── run.py              WSGI entry point (gunicorn run:app); `python run.py` starts the dev server
├── app/
│   ├── __init__.py     create_app(): configuration, extensions, CORS, JSON errors, blueprints
│   ├── config.py       settings from the environment (ProdConfig.from_env) and TestConfig
│   ├── extensions.py   SQLAlchemy, Flask-Login, Flask-Limiter, the MongoDB client
│   ├── models.py       User and StripeEvent (MySQL)
│   ├── decorators.py   subscription_required: login plus data access
│   ├── cli.py          flask init-db and flask make-admin
│   └── routes/         auth.py (accounts), stripe.py (billing, webhook), cortex.py (data)
├── pipeline/           offline data pipeline and the label-quality models (pipeline/labelqa)
├── tests/              web app tests; tests/pipeline holds the pipeline tests
├── docs/               API.md, DATA_MODEL.md, PIPELINE.md
├── requirements.txt    web app dependencies (pinned)
├── requirements-dev.txt  test, lint and audit tools
├── pytest.ini, ruff.toml
└── .env.example        configuration template
```

## Local development

You need Python 3.12 or 3.13, a MySQL 8 server and a MongoDB server.

```sh
cd backend
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
```

Create the MySQL database and user named in `.env`, for example:

```sql
CREATE DATABASE cortex CHARACTER SET utf8mb4;
CREATE USER 'cortex'@'127.0.0.1' IDENTIFIED BY '<password>';
GRANT ALL PRIVILEGES ON cortex.* TO 'cortex'@'127.0.0.1';
```

Then fill in `.env`:

- Set `SECRET_KEY`, `DB_PASSWORD` and the other blank values. The app refuses
  to start while a required variable is blank, and the error names every
  missing one.
- Uncomment `COOKIE_SECURE=0` (the dev server speaks plain http, and the
  browser would drop Secure cookies), and uncomment `TRUSTED_PROXY_COUNT` with
  the value `0` (no reverse proxy in front of the dev server).
- Without Stripe, Mailgun or Google accounts, any non-empty placeholder lets
  the app start. Checkout, reset emails and Google sign-in then fail, while
  email and password login and the dashboard work.

Create the tables and start the development server on
<http://127.0.0.1:5000>:

```sh
flask --app run init-db
flask --app run run            # or: python run.py
```

`flask` and `python run.py` both load `.env` (through python-dotenv from
`requirements-dev.txt`). Add `--debug` to `flask --app run run`, or set
`FLASK_DEBUG=1` for `python run.py`, for auto-reload. Errors still answer
JSON 500s (the app handles every exception); their tracebacks go to the server
log.

To use the dashboard without a subscription, sign up through the frontend
(see [../frontend/README.md](../frontend/README.md)) and give the account
complimentary access:

```sh
flask --app run make-admin you@example.com
```

The dashboard shows only images the pipeline has fully processed. Run the
pipeline against your local MongoDB, or insert documents shaped like the
example in [docs/DATA_MODEL.md](docs/DATA_MODEL.md#image-documents).

### Stripe webhooks on a local machine

Stripe cannot reach `localhost`, so forward its events with the
[Stripe CLI](https://docs.stripe.com/stripe-cli), using test-mode keys:

```sh
stripe login
stripe listen --forward-to localhost:5000/api/webhook \
  --events customer.subscription.created,customer.subscription.updated,customer.subscription.deleted,invoice.payment_succeeded,invoice.payment_failed,checkout.session.completed
```

`stripe listen` prints a webhook signing secret. Put it in
`STRIPE_WEBHOOK_SECRET` and restart the server.

## Configuration

The web app reads its configuration from the environment when `create_app()`
runs. [`.env.example`](.env.example) lists every variable with a comment; the
pipeline's variables are in [docs/PIPELINE.md](docs/PIPELINE.md#configuration).

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `SECRET_KEY` | yes | | Signs session cookies and reset tokens. Keep it stable across restarts and identical in all workers. |
| `FRONTEND_URL` | yes | | Public origin of the SPA: the only CORS origin, and the base of reset links. A trailing slash is stripped. |
| `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | yes | | MySQL connection (PyMySQL). The password is escaped, so any characters work. |
| `DB_PORT` | no | `3306` | MySQL port |
| `DB_SSL_CA` | no | unset | Path to a CA certificate; when set, MySQL connections use TLS verified against it |
| `MONGO_URI` | yes | | MongoDB connection string. The older name `mongo_db_uri` is still read when `MONGO_URI` is unset, with a deprecation warning. |
| `MONGO_DB`, `MONGO_COLLECTION` | no | `cortex`, `collection` | The image collection |
| `STRIPE_API_KEY` | yes | | Stripe secret key |
| `STRIPE_PRICE_ID` | yes | | Price of the subscription plan |
| `STRIPE_WEBHOOK_SECRET` | yes | | Signing secret of the webhook endpoint |
| `STRIPE_SUCCESS_URL` | yes | | Return URL after checkout (`?session_id=...` is appended) and from the billing portal; normally `<FRONTEND_URL>/dashboard` |
| `STRIPE_CANCEL_URL` | yes | | Return URL when checkout is cancelled |
| `MAILGUN_API_KEY`, `MAILGUN_DOMAIN` | yes | | Password-reset emails |
| `GOOGLE_CLIENT_ID` | yes | | OAuth client id that Google ID tokens must be issued to; the same value as the frontend's `REACT_APP_GOOGLE_CLIENT_ID` |
| `COOKIE_SECURE` | no | `1` | `0` drops the `Secure` flag from the login cookies; only for local http development |
| `TRUSTED_PROXY_COUNT` | no | `1` | Reverse proxies whose `X-Forwarded-For` entry is trusted for the client address; `0` when clients reach gunicorn directly |
| `RATELIMIT_STORAGE_URI` | no | `memory://` | Rate-limit counter storage. `memory://` counts per gunicorn worker; a `redis://` URI also needs the `redis` package installed. |
| `EXPORT_MAX_ROWS` | no | `100000` | Maximum rows of one CSV export (at least 1) |
| `FLASK_DEBUG` | no | off | `1` enables debug mode (auto-reload) for `python run.py`; tracebacks go to the server log; never in production |

## Accounts, sessions and access

- **Sign-in.** Users sign in with email and password or with Google (a Google
  ID token verified against `GOOGLE_CLIENT_ID`). An email that already has a
  password account cannot be signed in with Google.
- **Sessions.** A login lasts 30 days. The session and remember cookies are
  `HttpOnly`, `SameSite=Lax` and `Secure` (unless `COOKIE_SECURE=0`). The
  cookies carry the user's session token: logging out or resetting the
  password replaces it, which ends every session of that user on every device.
- **Password reset.** `POST /api/forgot-password` always gives the same answer
  and mails a link only to accounts that have a password. The link is valid
  for one hour and works once.
- **Rate limits.** Login, registration and Google sign-in allow 10 requests
  per minute per client IP; forgot-password allows 3 per hour per email
  address and 3 per hour per IP.
- **Data access.** `POST /api/get-labeled-data` needs a logged-in user whose
  `subscription_status` is `active` or `trialing`, or who has complimentary
  access (`flask --app run make-admin <email>`); everyone else gets 403. The
  Stripe webhook only updates `subscription_status`; access is decided from
  that column on every request, so a cancellation takes effect on the user's
  next request. The request's `query.source` key and the `error: broken URL`
  marker, which no query applied and no pipeline stage wrote, are gone: a
  `source` key is now ignored like any other unknown key (see
  [docs/API.md](docs/API.md#removed-request-fields)).
- **Billing.** Checkout returns a Stripe URL (`{url}`) and answers 409 while
  the user already has an active, trialing or past-due subscription. The first
  subscription starts with a one-day free trial. The billing portal always
  opens for the logged-in user's own Stripe customer.
- **CSV export.** The export holds the whole filtered result (not one page),
  at most `EXPORT_MAX_ROWS` rows, with cells that could run as spreadsheet
  formulas prefixed by an apostrophe. Its `hash` column is the SHA-256 of the
  image bytes, empty for rows stored before the pipeline recorded it.

[docs/API.md](docs/API.md) has the details, including the endpoints that no
longer exist.

## Command-line tools

| Command | Effect |
| --- | --- |
| `flask --app run init-db` | Creates the MySQL tables that do not exist yet; never alters existing ones |
| `flask --app run make-admin <email>` | Gives an existing account complimentary data access (the `superuser` flag) |
| `flask --app run routes` | Lists the registered endpoints |

They need the same environment as the app.

## Running in production

No deployment is maintained from this repository. To run the web app:

1. Install only the web requirements: `pip install -r requirements.txt`.
   python-dotenv is not among them, so provide the variables through the
   process environment (for example a systemd `EnvironmentFile`), not `.env`.
2. Create the tables once with `flask --app run init-db`. For an existing
   database whose `user` table has no `session_token` column, first follow
   [Upgrading an existing database](docs/DATA_MODEL.md#upgrading-an-existing-database).
3. Start gunicorn on a local port, for example
   `gunicorn --workers 4 --bind 127.0.0.1:5000 run:app`.
4. Put a reverse proxy in front of it:
   - terminate TLS there (the login cookies are `Secure`);
   - serve the frontend build at `/`, falling back to `index.html` for
     unknown paths so that client-side routes such as `/dashboard` and
     `/reset-password/<token>` load;
   - forward `/api/` to gunicorn, keeping the path and the request body
     unchanged (Stripe signs the raw body) and setting `X-Forwarded-For`.
     Nothing outside `/api/` needs to reach the backend;
   - set `TRUSTED_PROXY_COUNT` to the number of proxies that append to
     `X-Forwarded-For` (1 for a single proxy), so rate limits see the real
     client address.

External services:

- **Stripe.** Create the product and its recurring price (`STRIPE_PRICE_ID`),
  save the customer portal settings in the Stripe Dashboard, and add a webhook
  endpoint at `https://<your host>/api/webhook` subscribed to
  `customer.subscription.created`, `customer.subscription.updated`,
  `customer.subscription.deleted`, `invoice.payment_succeeded`,
  `invoice.payment_failed` and `checkout.session.completed`. Its signing
  secret is `STRIPE_WEBHOOK_SECRET`. A webhook that fails with a database
  error answers 500, and Stripe retries it.
- **Google.** Create an OAuth client of type "Web application" and add the
  SPA's origin (and `http://localhost:3000` for development) as an authorised
  JavaScript origin. Its client id goes into both `GOOGLE_CLIENT_ID` and the
  frontend's `REACT_APP_GOOGLE_CLIENT_ID`.
- **Mailgun.** Use a verified sending domain. Mail goes out from
  `mailgun@<MAILGUN_DOMAIN>` through Mailgun's US API endpoint
  (`api.mailgun.net`); a domain in Mailgun's EU region needs that URL changed
  in `app/routes/auth.py`.

With `RATELIMIT_STORAGE_URI=memory://` every gunicorn worker counts on its own,
so the effective limits are multiplied by the number of workers; use a shared
store such as Redis for exact limits.

## Tests and linting

The CI workflow (`.github/workflows/backend.yml`) runs these commands on
Python 3.12 and 3.13:

```sh
cd backend
pip install -r requirements.txt -r requirements-dev.txt
ruff check .
pytest -q -m "not pipeline" --cov=app
pip-audit -r requirements.txt
```

The web app tests need no services and no environment variables: they use
`TestConfig` with an in-memory SQLite database, an in-memory MongoDB
(mongomock) and mocked Stripe, Google and Mailgun calls. The pipeline tests
need the pipeline requirements and are run separately; see
[docs/PIPELINE.md](docs/PIPELINE.md#tests).

## Licence

AGPL-3.0-only, like the rest of the repository; see [LICENSE](../LICENSE) and
[NOTICE](../NOTICE).
