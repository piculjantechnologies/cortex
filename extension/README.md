# Cortex browser extension

A Chrome extension (Manifest V3) that shows what Cortex knows about the images
of the page you are viewing: the Pascal VOC boxes the pipeline detected and the
label-quality score, drawn over each image. Images Cortex does not have yet can
be sent for analysis with one click.

## Using it

1. In the extension's options (opened on install), enter the address of
   your Cortex deployment, then log in on its web app with an account that
   has data access (an active subscription or complimentary access). The
   extension uses that login; it has no password of its own.
2. Click the Cortex button in the toolbar. The extension looks up every image
   of at least 64 × 64 px on the page and, for each one Cortex knows, draws
   its boxes (in the gallery's class colours) and a badge with the score:
   green from 80 %, amber from 50 %, red below.
3. An image Cortex does not have gets an **Analyse** button; the panel's
   **Analyse N new** button sends all of them. The badge then shows
   *Queued…* and *Scoring…* and turns into the score once the pipeline has
   processed the image. The extension checks every 5 seconds for up to
   15 minutes.
4. Click the toolbar button again, or × in the panel, to remove everything.

The panel shows how many images have results, are being analysed or are new,
and why a request failed (not logged in, no subscription, rate limit, Cortex
unreachable).

## How it works

- **Nothing runs until you click.** The extension has no content script on
  every page: a click injects `lib/shared.js` and `content.js` into the
  current tab only (`activeTab` + `scripting`).
- **The page never talks to Cortex.** The page script sends image URLs to the
  service worker (`background.js`), which calls the API with the browser's
  Cortex session cookie (`credentials: 'include'`). Only the images'
  addresses are sent, never the page's.
- **The page's layout is not changed.** Each image's boxes and badge live in
  a small layer placed next to the image, in the same containing element: it
  is absolutely positioned, keeps its styles in a closed shadow root, and is
  removed when you close the panel. Because the layer sits where the image
  sits, the page's own scrolling, clipping and stacking apply to it. A sticky
  header or the edge of a scrolled panel hides the layer where it hides the
  image. The boxes follow the image's `object-fit` and `object-position`.
  Hidden images get no layer. Where images are stacked (a preview over its
  placeholder, a carousel's slides), only the one in front gets a layer.
  Images added later (lazy loading, infinite scroll) are picked up.
- **API.** `POST /api/images/lookup` (up to 50 URLs per call) and
  `POST /api/images/analyse`, described in
  [backend/docs/API.md](../backend/docs/API.md#browser-extension). Analysing
  only queues the URL: the pipeline's detection and scoring stages take
  requested images before the crawl's, so they need to be running (see
  [PIPELINE.md](../backend/docs/PIPELINE.md)).

## Settings

Right-click the toolbar button → **Options**:

| Setting | Meaning |
| --- | --- |
| Cortex API URL | where the API calls go: the server that answers `/api/…`, usually the web app's address (required) |
| Cortex web app URL | opened by "Open Cortex" to log in; empty means the API URL |

No deployment is built in. The extension holds no host permission at
install: saving the options asks for access to the one origin you entered
(match patterns ignore the port, so `http://localhost:5000` asks for
`http://localhost/*`). An address that redirects instead of answering, or
that cannot be reached, is reported in the panel with a link to the options.

## Installing from source

1. Open `chrome://extensions` and turn on **Developer mode**.
2. **Load unpacked** and choose this `extension/` folder.
3. For a local Cortex, open the extension's options and set the API URL to
   `http://localhost:5000` and the web app URL to `http://localhost:3000`,
   then log in on the web app.

## Development

The extension has no build step and no dependencies. `lib/shared.js` holds
the logic that needs no browser (picture geometry, box mapping, badges,
settings URLs) and is unit-tested with Node 22:

```sh
node --test extension/test/*.test.js
```

CI (`.github/workflows/extension.yml`) runs the tests, a syntax check of
every script and a manifest check. The test suite also checks that the class
colours match the web app's.
