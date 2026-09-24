# Cortex

Cortex is a web image scraper for object detection. Its offline pipeline
takes web pages listed in the [Common Crawl](https://commoncrawl.org) URL
index, collects the image URLs on them, detects the 20 Pascal VOC object
classes in each image, and gives each image a label-quality score: the
estimated probability that its boxes are correct. It stores the image URLs
with their boxes and scores, not the images. Subscribers search what it has
scraped by object class, image size and score, browse the results with their
boxes, and export them as CSV. A browser extension shows the same boxes and
scores on the images of any web page and sends images Cortex has not scraped
yet to its pipeline.

Object detection on images, with the 20 Pascal VOC classes, is where Cortex
starts. The plan is to extend it to other tasks (for example classification
and segmentation), more classes and other modalities (for example video,
audio and text).

**Status:** CI (GitHub Actions) builds and tests the backend, the frontend
and the browser extension; it deploys nothing. No deployment is maintained from this
repository.

## Label quality

The label-quality score is computed with the label quality assurance method.
Its maintained implementation, with training code and released checkpoints,
is at <https://github.com/piculjantechnologies/label-quality-assurance>. The
method is described in:

- Pičuljan, N. and Car, Ž. *Machine Learning-Based Label Quality Assurance for
  Object Detection Projects in Requirements Engineering.* Applied Sciences
  13(10):6234, 2023. [doi:10.3390/app13106234](https://doi.org/10.3390/app13106234)
- Pičuljan, N. *Machine learning-based method for quality assurance of object
  bounding box labels in images.* PhD thesis, University of Zagreb, Faculty of
  Electrical Engineering and Computing, 2025.
  [urn:nbn:hr:168:865107](https://urn.nsk.hr/urn:nbn:hr:168:865107)

Cortex can score with each of the three checkpoints released in that
repository, selected by `LABELQA_MODEL`: `unified` (the default), `thesis` or
`paper`. Their networks and preprocessing are vendored in
[`backend/pipeline/labelqa`](backend/pipeline/labelqa); the selected checkpoint
(106 to 155 MB) is not stored here but downloaded, and checked against its
SHA-256, when the scoring stage first runs.
[MODEL.md](backend/pipeline/labelqa/MODEL.md) gives each model's source, hash,
input and scoring contract and published evaluation.

## Repository layout

```
.
├── backend/                 Flask API, offline pipeline, tests
│   ├── app/                 web app (accounts, billing, data API)
│   ├── pipeline/            offline pipeline and the label-quality model
│   ├── docs/                API.md, DATA_MODEL.md, PIPELINE.md
│   └── README.md
├── frontend/                React single-page app
│   └── README.md
├── extension/               Chrome extension (Manifest V3)
│   └── README.md
├── .github/workflows/       CI: backend.yml, frontend.yml, extension.yml
├── LICENSE                  GNU AGPL v3
└── NOTICE                   copyright, scope of the licence, third-party credits
```

## User journey

1. **Sign up** with an email and password, or sign in with Google.
2. **Subscribe.** The dashboard offers a "Subscribe" button that opens Stripe
   Checkout. The first subscription starts with a one-day free trial that needs
   no card up front. Stripe returns the user to the dashboard, which waits
   until Stripe's webhook has confirmed the subscription.
3. **Search** by the object classes the image must contain (all of them or
   any of them) or must not contain, a minimum width and height, and a minimum
   label-quality score (defaults: any class, width 100 px, height 0, score
   50 %).
4. **Browse** the results, 25 per page, best label quality first (or newest or
   oldest first), each image shown from its original URL with its boxes and
   score.
5. **Export** the whole filtered result as CSV (up to 100000 rows by default).
6. **Manage billing** in the Stripe customer portal ("Open Stripe Portal"),
   where the subscription can be cancelled; data access ends as soon as
   Stripe reports the subscription as no longer active or trialing.
7. **Forgot password** sends a reset link valid for one hour; **Logout** ends
   the user's sessions on every device.
8. **Browse the web with the extension** (optional): on any page, its toolbar
   button draws the boxes and score of every image Cortex knows and offers
   "Analyse" for the others, which the pipeline then processes first.

## Architecture

```
Browser ── React SPA (static build) ─┐          images load directly from their original hosts
                                     ▼
                              reverse proxy (TLS)
                    /  → frontend build (index.html fallback)
                    /api/ → gunicorn run:app (Flask)
                                     │
            ┌────────────────────────┼─────────────────────────┐
            ▼                        ▼                         ▼
   MySQL: users,             MongoDB: image           Stripe (Checkout, portal,
   subscription status,      collection               webhook → /api/webhook),
   Stripe events                    ▲                  Mailgun (reset emails),
                                    │                  Google (ID token certificates)
                                    │
   offline pipeline (python -m pipeline.<stage>):
   Common Crawl index → Azure Blob Storage → pages → image URLs
     → object detection (boxes, size, hash) → label-quality score
```

- The **frontend** is a static build served at `/`. It calls the API on its
  own origin, so the session cookies stay first-party.
- The **reverse proxy** terminates TLS, serves the build (answering unknown
  paths with `index.html`) and forwards `/api/` to gunicorn. Nothing outside
  `/api/` reaches the backend, and the Stripe webhook arrives through the same
  path.
- The **web app** (`gunicorn run:app`) keeps accounts, subscription status and
  processed Stripe events in **MySQL**, and reads the image collection from
  **MongoDB**. Stripe's webhook updates a user's subscription status; data
  access is checked against it on every request.
- The **pipeline** runs separately and writes the MongoDB collection in four
  stages: download the index files, collect image URLs, detect objects, score
  the labels. It identifies itself, obeys robots.txt and throttles itself per
  host. Images a user sends from the browser extension are queued with a
  request time and detected and scored before the crawl's.
- The **browser extension** calls `/api/images/lookup` and
  `/api/images/analyse` from its service worker with the user's session
  cookie; the page it runs on never talks to Cortex.

## Quick start

Prerequisites: Python 3.12 or 3.13, Node 22, a MySQL 8 server and a MongoDB
server. Stripe, Mailgun and Google credentials are needed only for checkout,
reset emails and Google sign-in; without them, any non-empty placeholder in
`.env` lets the app start.

Backend (details in [backend/README.md](backend/README.md#local-development)):

```sh
cd backend
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env         # fill in; for local http also set COOKIE_SECURE=0 and TRUSTED_PROXY_COUNT=0
flask --app run init-db      # create the MySQL tables
flask --app run run          # API on http://127.0.0.1:5000
```

Frontend (details in [frontend/README.md](frontend/README.md#development)),
in a second terminal:

```sh
cd frontend
npm ci
cp .env.example .env.local   # set REACT_APP_API_URL=http://localhost:5000
npm start                    # SPA on http://localhost:3000
```

Sign up at <http://localhost:3000/signup>, then give the account data access
without a subscription:

```sh
cd backend
flask --app run make-admin you@example.com
```

The dashboard lists only images the pipeline has processed; see
[backend/docs/PIPELINE.md](backend/docs/PIPELINE.md) to fill the collection.

## Configuration

Each app is configured through environment variables, listed with their
defaults in:

- [backend/.env.example](backend/.env.example) and the table in
  [backend/README.md](backend/README.md#configuration). The web app refuses to
  start while a required variable is missing and names every missing one.
- [frontend/.env.example](frontend/.env.example) and the table in
  [frontend/README.md](frontend/README.md#configuration). These are inlined at
  build time.
- The pipeline's variables in
  [backend/docs/PIPELINE.md](backend/docs/PIPELINE.md#configuration).

## Tests and CI

Three GitHub Actions workflows run on pushes to `main` and on pull requests
that touch their part, and can be started by hand (`workflow_dispatch`).
They build and test only: they use no secrets and deploy nothing.

Backend (`.github/workflows/backend.yml`, Python 3.12 and 3.13):

```sh
cd backend
pip install -r requirements.txt -r requirements-dev.txt
ruff check .
pytest -q -m "not pipeline" --cov=app
pip-audit -r requirements.txt
```

Frontend (`.github/workflows/frontend.yml`, Node from `frontend/.nvmrc`):

```sh
cd frontend
npm ci
CI=true npm test -- --watchAll=false
CI=true npm run build
```

Extension (`.github/workflows/extension.yml`, the same Node version):

```sh
for f in extension/*.js extension/lib/*.js; do node --check "$f"; done
node --test extension/test/*.test.js
```

The pipeline tests need PyTorch and run outside CI:
`pytest -q -m pipeline tests/pipeline` in the pipeline environment (see
[backend/docs/PIPELINE.md](backend/docs/PIPELINE.md#tests)).

## Documentation

| Document | Contents |
| --- | --- |
| [backend/README.md](backend/README.md) | backend setup, configuration, accounts and access, production notes |
| [backend/docs/API.md](backend/docs/API.md) | HTTP API reference |
| [backend/docs/DATA_MODEL.md](backend/docs/DATA_MODEL.md) | MySQL tables, MongoDB documents, database upgrades |
| [backend/docs/PIPELINE.md](backend/docs/PIPELINE.md) | pipeline stages, crawl policy, operation |
| [backend/pipeline/labelqa/MODEL.md](backend/pipeline/labelqa/MODEL.md) | the three label-quality models and their checkpoints |
| [frontend/README.md](frontend/README.md) | screens, frontend configuration, development, deployment contract |
| [extension/README.md](extension/README.md) | the Chrome extension: use, permissions and privacy, settings, development |

## Licence

Copyright 2026 Neven Pičuljan.

Cortex is free software under the GNU Affero General Public License, version 3
only (AGPL-3.0-only); see [LICENSE](LICENSE). The licence covers all code in
this repository. The label-quality checkpoints that the pipeline downloads are
released under the same licence in the label-quality-assurance repository;
their image trunks are derived from torchvision's ImageNet-pretrained ResNets
and remain subject to torchvision's BSD 3-Clause notice reproduced in
[NOTICE](NOTICE).
NOTICE also lists the scope, the other third-party components and the terms
that apply to Common Crawl data and to the collected images.
