# Cortex frontend

The single-page app of Cortex, built with Create React App (`react-scripts`
5), React 18 and React Router 6. Users sign up or log in, subscribe through
Stripe Checkout, search the image collection by object class, image size and
label-quality score, browse the results with their bounding boxes, and
download the whole filtered result as CSV.

The app talks only to the Flask backend in [`../backend`](../backend); the
endpoints it uses are documented in
[`../backend/docs/API.md`](../backend/docs/API.md).

## Screens

| Path | Screen | Notes |
| --- | --- | --- |
| `/` | redirect | to `/dashboard` when logged in, otherwise to `/login` |
| `/login` | Log in | email and password, "Sign in with Google", links to sign-up and forgot-password; a logged-in user is sent to `/dashboard` |
| `/signup` | Sign up | email, password and confirmation (6 to 128 characters); on success it returns to `/login` with a notice |
| `/forgot-password` | Forgot password | asks for the email; always shows the same neutral message |
| `/reset-password/:token` | Reset password | the link from the reset email; sets a new password and returns to `/login` |
| `/dashboard` | Dashboard | logged-in users only (others are sent to `/login`); see below |
| anything else | redirect | to `/` |

The dashboard first asks the backend whether the user has data access, then
shows one of three states:

- **No subscription:** a "Subscribe" button that starts Stripe Checkout. A
  user who already has a Stripe customer (for example after a failed payment)
  also gets "Open Stripe Portal" to update the payment method.
- **Subscribed** (or complimentary access): the search form, the gallery with
  pagination, "Download CSV" and "Open Stripe Portal".
- **Error:** the message and a "Try again" button.

The header shows the user's email and a "Logout" button. Checkout replaces the
dashboard (Stripe sends the user back to it); "Open Stripe Portal" opens the
portal in a new tab, or in the same tab if the browser blocks the new one.

## How it works

**Backend calls.** Every request goes through `src/api/client.js`, which
prefixes `REACT_APP_API_URL`, sends JSON, includes the session cookies
(`credentials: 'include'`; sign-up, forgot-password and reset-password send
none), and turns a non-2xx answer into an error carrying the backend's
`message` (or `error`) text.

**Authentication.** The session lives only in the backend's HttpOnly cookies;
the app keeps no token of its own. At start-up `AuthContext` calls
`GET /api/check-auth`, and any later 401 from the backend marks the user as
logged out. Google sign-in uses Google Identity Services
(`@react-oauth/google`): the button returns a signed ID token, which the app
posts to `POST /api/authorize/google` as `{credential}`. The reset screen
takes the token from its URL (`/reset-password/:token`) and posts
`{token, password}` to `POST /api/reset-password`, without cookies; the token
never goes into an API path. On a 200 it returns to `/login`. Logging out ends
the user's sessions on every device.

**Subscription.** "Subscribe" sends `POST /api/create-checkout-session` and
"Open Stripe Portal" sends `POST /api/create-portal-session`, both with no
body; each answers `{url}`. Checkout replaces the dashboard
(`window.location.assign(url)`); the portal opens in a tab that is opened
during the click (so popup blockers allow it) and pointed at the URL once it
arrives, or in the same tab if the browser refused the new one; the empty tab
is closed if the request fails. After Checkout, Stripe sends the user back to
`/dashboard?session_id=...`. Because the Stripe webhook may arrive a moment
later, the dashboard then polls `/api/check-subscription-status` once a second
for up to 10 attempts before showing "Your payment is still being confirmed".

**Search.** The form builds a `filter` in the backend's filter language
([API.md](../backend/docs/API.md#filter-language)) and sends it with `sort` to
`POST /api/get-labeled-data`. It has two tabs.

*Filters* (the default):

| Field | Default | Adds to the filter |
| --- | --- | --- |
| Classes | every class neutral | one chip per class; a click cycles it neutral -> included (✓) -> excluded (✕) -> neutral. An included class adds `{"class": ...}`, an excluded one `{"$not": {"class": ...}}`; "Clear" resets every chip |
| Match all / Match any | Match all | with Match any, the included classes are combined under `$or` |
| Min objects | 1 | `count: {"$gte": n}` on each included class |
| Largest box at least | 0 % | `max_box_area: {"$gte": n / 100}` on each included class |
| Only these classes | off | excludes every class that is not included |
| Min width, Min height | 100, 0 | `width`, `height` (`$gte`); nothing when 0 |
| Min label quality | 50 | `label_quality: {"$gte": n / 100}`; the slider and the number field show the same value |
| Sort by | Best label quality | `sort`: `quality`, `newest` or `id` ("Oldest") |

The last three class options are enabled once a class is included.

*Advanced* shows the filter as JSON in an editor. It starts from the filters
of the first tab the first time it opens (later edits are kept); the "Start
from" buttons load the first tab's filters again or one of three examples. The editor says whether the text is valid JSON, a
"Filter reference" lists the language, and the backend's message for an
invalid filter names the offending part (e.g. `filter.$and[1].count`).

An empty or invalid number falls back to its default. The form applies on
"Search"; pagination and the CSV keep using the last submitted search. Results
come 25 per page (the backend's default page size) with Previous and Next
buttons, under a count of the matching images. Each image is loaded directly
from its original URL, with its boxes drawn over it in one colour per class
(the chips show the same colours), the classes it contains with their box
counts, and its label-quality score as a badge: green from 80 %, amber from
50 %, red below. Clicking the image opens the original in a new tab. Only
absolute `http(s)` URLs are rendered as images or links.

**CSV download.** "Download CSV" is enabled after the first search and
exports the last submitted search (not unsubmitted form values). The backend
streams every matching row, up to its `EXPORT_MAX_ROWS` cap (100000 by
default), and the browser saves it as `data.csv`. Columns: `_id`, `url`,
`width`, `height`, `hash`, `object_detection` (JSON), `label_quality_score`
(0 to 1). The backend prefixes cells that could run as spreadsheet formulas
with an apostrophe.

## Configuration

Copy [`.env.example`](.env.example) to `.env.local` (ignored by git) and fill
it in. Create React App reads the variables at `npm start` and inlines them at
`npm run build`, so set them before building. They end up in the public
bundle: never put secrets in them.

| Variable | Default | Purpose |
| --- | --- | --- |
| `REACT_APP_API_URL` | empty | Base URL of the backend. Empty means the SPA's own origin, which is how the production build is served. For local development: `http://localhost:5000`. |
| `REACT_APP_GOOGLE_CLIENT_ID` | empty | OAuth client id for "Sign in with Google", the same value as the backend's `GOOGLE_CLIENT_ID`. Without it only email and password login works. |

## Development

Use Node 22 (`.nvmrc`; `nvm use` picks it up).

```sh
cd frontend
npm ci
cp .env.example .env.local   # then set REACT_APP_API_URL=http://localhost:5000
npm start                    # http://localhost:3000
```

The backend must be running on port 5000 with
`FRONTEND_URL=http://localhost:3000` (the only origin its CORS allows) and
`COOKIE_SECURE=0` (the development servers use plain http); see
[`../backend/README.md`](../backend/README.md#local-development). For Google
sign-in, `http://localhost:3000` must be an authorised JavaScript origin of the
OAuth client.

| Command | Effect |
| --- | --- |
| `npm start` | development server with reload on <http://localhost:3000> |
| `npm test` | Jest in interactive watch mode |
| `npm run test:ci` | all tests once |
| `npm run build` | production build in `build/` |

`npm ci` reports npm audit advisories. Every high-severity one is in Create
React App's build and test tooling (`react-scripts` 5), which runs on the
developer's machine and in CI but is not part of the files in `build/`;
`npm audit fix` cannot resolve them without moving off Create React App. The
two moderate React Router 6 advisories are fixed only in React Router 7; they
concern server rendering and links built from user input, and this app does
neither (it only navigates to fixed paths).

The tests use React Testing Library with the API client (or `fetch`) mocked,
and any unexpected `console.error` or `console.warn` fails the test that
caused it (`jest-fail-on-console`). The CI workflow
(`.github/workflows/frontend.yml`) runs:

```sh
cd frontend
npm ci
CI=true npm test -- --watchAll=false
CI=true npm run build
```

With `CI=true` the build treats ESLint warnings as errors.

## Deployment contract

No deployment is maintained from this repository. The production build
expects to share one origin with the backend behind a reverse proxy:

- the proxy serves the contents of `build/` at `/` and answers every unknown
  path with `index.html`, so client-side routes such as `/dashboard` and
  `/reset-password/<token>` load directly;
- it forwards `/api/` to the backend (gunicorn); nothing else goes to the
  backend;
- the build is made with `REACT_APP_API_URL` empty, so every call goes to
  `/api/...` on the same origin and the session cookies stay first-party.

The app is built for the site root (`/`); serving it under a sub-path would
need the `homepage` field in `package.json`.

## Licence

AGPL-3.0-only, like the rest of the repository; see [LICENSE](../LICENSE) and
[NOTICE](../NOTICE).
