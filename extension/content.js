// Page script, injected when the toolbar button is clicked. It collects the page's
// images, asks Cortex (through the service worker) what it knows about them and draws
// the boxes and label-quality badges on them. Each image's layer is placed next to the
// image, in the same containing element, so the page's own scrolling, clipping and
// stacking apply to it: a sticky header or the edge of a scrolled panel hides the layer
// exactly where it hides the image. Layers are absolutely positioned, with their styles
// in a closed shadow root, so they do not change the page's layout. Injected again, the
// script removes itself.
(function () {
  'use strict';

  if (window.__cortexOverlay) {
    window.__cortexOverlay.close();
    return;
  }

  const shared = window.CortexShared;
  const MIN_SIZE = 64; // px, rendered and natural: smaller images are icons and spacers
  const LOOKUP_BATCH = 50; // the API's limit per lookup
  const POLL_MS = 5000;
  const POLL_LIMIT_MS = 15 * 60 * 1000;
  const STACKED = 0.6; // images sharing this much of the larger one's area are copies in one spot
  const LAYER_TAG = 'cortex-layer';

  const results = new Map(); // image URL -> lookup result
  const layers = new Map(); // <img> -> {element, frame, key, badge}
  const pendingSince = new Map(); // image URL -> time polling started
  let showBoxes = true;
  let message = '';
  let closed = false;

  // --- the panel, isolated from the page's styles ------------------------------------

  const host = document.createElement('cortex-overlay');
  host.style.cssText = 'position:absolute;top:0;left:0;width:0;height:0;z-index:2147483646;pointer-events:none;';
  const shadow = host.attachShadow({ mode: 'closed' });
  shadow.innerHTML = `
    <style>
      :host { all: initial; }
      * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
      .panel { position: fixed; top: 12px; right: 12px; width: 280px; padding: 12px 14px; border: 1px solid #e3e6eb;
               border-radius: 12px; background: #fff; color: #14171f; box-shadow: 0 8px 24px rgba(20,23,31,.18);
               font-size: 13px; pointer-events: auto; }
      .panel header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
      .panel strong { font-size: 14px; }
      .panel .close { border: none; background: none; color: #5f6673; font-size: 18px; line-height: 1; cursor: pointer; }
      .panel p { margin: 4px 0; color: #5f6673; }
      .panel .message { color: #b03427; }
      .panel .actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
      .panel button.action { padding: 5px 10px; border: 1px solid #cfd4dc; border-radius: 8px; background: #fff;
                             color: #14171f; font-size: 12px; font-weight: 600; cursor: pointer; }
      .panel button.action.primary { border-color: #14171f; background: #14171f; color: #fff; }
      .panel button.action:disabled { opacity: .45; cursor: default; }
    </style>
    <section class="panel" role="dialog" aria-label="Cortex">
      <header><strong>Cortex</strong><button class="close" title="Close" aria-label="Close">×</button></header>
      <p class="counts">Looking for images…</p>
      <p class="message" hidden></p>
      <div class="actions">
        <button class="action primary analyse-all" hidden></button>
        <button class="action toggle-boxes">Hide boxes</button>
        <button class="action login" hidden>Open Cortex</button>
        <button class="action options" hidden>Options</button>
      </div>
    </section>`;
  const panel = {
    counts: shadow.querySelector('.counts'),
    message: shadow.querySelector('.message'),
    analyseAll: shadow.querySelector('.analyse-all'),
    toggleBoxes: shadow.querySelector('.toggle-boxes'),
    login: shadow.querySelector('.login'),
    options: shadow.querySelector('.options'),
  };
  document.documentElement.appendChild(host);

  // --- an image's layer ----------------------------------------------------------------

  const LAYER_CSS = `
    * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    .frame { position: absolute; overflow: hidden; pointer-events: none; }
    .box { position: absolute; border: 2px solid; border-radius: 2px; box-shadow: 0 0 0 1px rgba(255,255,255,.35); }
    .box span { position: absolute; top: -2px; left: -2px; padding: 0 5px; border-radius: 2px 2px 2px 0;
                font-size: 11px; font-weight: 600; line-height: 16px; white-space: nowrap; }
    .badge { position: absolute; top: 6px; right: 6px; padding: 3px 8px; border: none; border-radius: 999px;
             font-size: 12px; font-weight: 700; line-height: 16px; white-space: nowrap; pointer-events: auto;
             box-shadow: 0 1px 3px rgba(0,0,0,.25); }
    .good { background: #e6f5ec; color: #1f7a45; }
    .fair { background: #fcf1d6; color: #8a5d00; }
    .poor { background: #fbe8e5; color: #b03427; }
    .none, .failed, .pending { background: rgba(255,255,255,.92); color: #5f6673; font-weight: 600; }
    .unknown { background: #14171f; color: #fff; cursor: pointer; }
    .unknown:hover { background: #2e3440; }
    .unknown:disabled { opacity: .6; cursor: default; }
    .pending::before { content: ""; display: inline-block; width: 8px; height: 8px; margin-right: 6px;
                       border: 2px solid #cfd4dc; border-top-color: #14171f; border-radius: 50%;
                       vertical-align: -1px; animation: spin .8s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }`;

  // An empty, zero-size element at the top left of its containing block; the frame inside
  // it covers the image. The page's style sheets cannot reach into it.
  function createLayer() {
    const element = document.createElement(LAYER_TAG);
    element.style.cssText =
      'all:initial !important;display:block !important;position:absolute !important;left:0 !important;' +
      'top:0 !important;width:0 !important;height:0 !important;overflow:visible !important;' +
      'pointer-events:none !important;';
    const root = element.attachShadow({ mode: 'closed' });
    root.innerHTML = `<style>${LAYER_CSS}</style><div class="frame"></div>`;
    return { element, frame: root.querySelector('.frame'), key: '', badge: null };
  }

  // The element the layer goes into: the image's parent (the <picture>'s, for a <picture>).
  function containerOf(image) {
    const parent = image.parentElement;
    return parent && parent.tagName === 'PICTURE' ? parent.parentElement : parent;
  }

  function removeLayer(image) {
    layers.get(image).element.remove();
    layers.delete(image);
    resizes.unobserve(image);
  }

  // --- talking to the service worker --------------------------------------------------

  function send(payload) {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage(payload, (reply) => {
        resolve(reply || { ok: false, status: 0, error: 'The Cortex extension did not answer.' });
      });
    });
  }

  function reportError(reply) {
    message = reply.error;
    panel.login.hidden = reply.status !== 401;
    panel.options.hidden = !reply.setup;
    renderPanel();
  }

  async function lookup(urls) {
    for (const batch of shared.chunk(urls, LOOKUP_BATCH)) {
      const reply = await send({ type: 'lookup', urls: batch });
      if (closed) {
        return;
      }
      if (!reply.ok) {
        reportError(reply);
        return;
      }
      message = '';
      panel.login.hidden = true;
      panel.options.hidden = true;
      for (const [url, result] of Object.entries(reply.data.results)) {
        results.set(url, result);
        if (shared.isPending(result)) {
          if (!pendingSince.has(url)) {
            pendingSince.set(url, Date.now());
          }
        } else {
          pendingSince.delete(url);
        }
      }
    }
    render();
  }

  async function analyse(url) {
    const reply = await send({ type: 'analyse', url });
    if (closed) {
      return false;
    }
    if (!reply.ok) {
      reportError(reply);
      return false;
    }
    const { url: _storedUrl, ...result } = reply.data;
    results.set(url, result);
    if (shared.isPending(result)) {
      pendingSince.set(url, Date.now());
    }
    render();
    return true;
  }

  // --- the page's images ---------------------------------------------------------------

  function imageUrl(image) {
    const url = image.currentSrc || image.src;
    return /^https?:\/\//i.test(url) ? url : null;
  }

  // Not hidden by visibility, opacity or content-visibility, on itself or an ancestor
  // (a preview's placeholder under the full image, a faded-out slide).
  function isShown(image) {
    return (
      typeof image.checkVisibility !== 'function' ||
      image.checkVisibility({ opacityProperty: true, visibilityProperty: true, checkOpacity: true, checkVisibilityCSS: true })
    );
  }

  // The images worth a layer, each with its rectangle in the viewport.
  function candidates() {
    const found = [];
    for (const image of document.images) {
      const rect = image.getBoundingClientRect();
      if (
        imageUrl(image) &&
        image.complete &&
        image.naturalWidth >= MIN_SIZE &&
        image.naturalHeight >= MIN_SIZE &&
        rect.width >= MIN_SIZE / 2 &&
        rect.height >= MIN_SIZE / 2 &&
        containerOf(image) &&
        isShown(image)
      ) {
        found.push({ image, rect });
      }
    }
    return frontmost(found);
  }

  // Of images drawn on top of each other (a carousel's slides, a zoomed copy over its
  // thumbnail), only the one in front keeps a layer.
  function frontmost(found) {
    const behind = new Set();
    for (let i = 0; i < found.length; i++) {
      for (let j = i + 1; j < found.length; j++) {
        const a = found[i];
        const b = found[j];
        if (!behind.has(a.image) && !behind.has(b.image) && shared.overlapRatio(a.rect, b.rect) >= STACKED) {
          behind.add(inFront(a, b) === a ? b.image : a.image);
        }
      }
    }
    return found.filter(({ image }) => !behind.has(image));
  }

  function inFront(a, b) {
    const x = (Math.max(a.rect.left, b.rect.left) + Math.min(a.rect.right, b.rect.right)) / 2;
    const y = (Math.max(a.rect.top, b.rect.top) + Math.min(a.rect.bottom, b.rect.bottom)) / 2;
    const stack = document.elementsFromPoint(x, y); // topmost first
    const ia = stack.indexOf(a.image);
    const ib = stack.indexOf(b.image);
    if (ia !== -1 && (ib === -1 || ia < ib)) {
      return a;
    }
    if (ib !== -1) {
      return b;
    }
    // Neither can be hit (off screen, or pointer-events: none): the later one is painted over.
    return a.image.compareDocumentPosition(b.image) & Node.DOCUMENT_POSITION_FOLLOWING ? b : a;
  }

  function scan() {
    if (closed) {
      return;
    }
    const fresh = [...new Set(candidates().map(({ image }) => imageUrl(image)))].filter((url) => !results.has(url));
    if (fresh.length) {
      fresh.forEach((url) => results.set(url, { status: 'looking' }));
      lookup(fresh);
    } else {
      render();
    }
  }

  function poll() {
    if (closed) {
      return;
    }
    const now = Date.now();
    const due = [...pendingSince.entries()].filter(([, since]) => now - since < POLL_LIMIT_MS).map(([url]) => url);
    if (due.length) {
      lookup(due);
    }
  }

  // --- drawing ---------------------------------------------------------------------------

  // The image's content box (inside border and padding), in viewport pixels.
  function contentRect(image, rect) {
    const style = getComputedStyle(image);
    const px = (name) => parseFloat(style.getPropertyValue(name)) || 0;
    const left = rect.left + px('border-left-width') + px('padding-left');
    const top = rect.top + px('border-top-width') + px('padding-top');
    const width = rect.width - px('border-left-width') - px('border-right-width') - px('padding-left') - px('padding-right');
    const height = rect.height - px('border-top-width') - px('border-bottom-width') - px('padding-top') - px('padding-bottom');
    return { left, top, width, height, right: left + width, bottom: top + height, fit: style.objectFit, position: style.objectPosition };
  }

  // The part of the box left visible by ancestors that clip without scrolling (overflow
  // hidden or clip), such as a rounded frame narrower than its image. Scrolled panels are
  // left out: the badge scrolls with its image there.
  function unclipped(image, box) {
    const visible = { left: box.left, top: box.top, right: box.right, bottom: box.bottom };
    for (let element = image.parentElement; element && element !== document.body; element = element.parentElement) {
      const style = getComputedStyle(element);
      const clipsX = style.overflowX === 'hidden' || style.overflowX === 'clip';
      const clipsY = style.overflowY === 'hidden' || style.overflowY === 'clip';
      if (clipsX || clipsY) {
        const r = element.getBoundingClientRect();
        if (clipsX) {
          visible.left = Math.max(visible.left, r.left);
          visible.right = Math.min(visible.right, r.right);
        }
        if (clipsY) {
          visible.top = Math.max(visible.top, r.top);
          visible.bottom = Math.min(visible.bottom, r.bottom);
        }
      }
    }
    return visible.right > visible.left && visible.bottom > visible.top ? visible : box;
  }

  function drawLayer(image, rect, result) {
    let layer = layers.get(image);
    if (!layer) {
      layer = createLayer();
      layers.set(image, layer);
      resizes.observe(image);
    }
    const container = containerOf(image);
    if (layer.element.parentNode !== container) {
      container.appendChild(layer.element);
    }
    const box = contentRect(image, rect);
    // Both rectangles are in viewport pixels, so the difference holds at any scroll position.
    const origin = layer.element.getBoundingClientRect();
    Object.assign(layer.frame.style, {
      left: `${box.left - origin.left}px`,
      top: `${box.top - origin.top}px`,
      width: `${box.width}px`,
      height: `${box.height}px`,
    });

    const url = imageUrl(image);
    const key = [url, result.status, result.label_quality_score, showBoxes, box.width, box.height, box.fit, box.position].join('|');
    if (layer.key !== key) {
      layer.key = key;
      layer.badge = fillLayer(layer.frame, image, box, url, result);
    }
    if (layer.badge) {
      const inset = shared.badgeInset(box, unclipped(image, box));
      layer.badge.style.top = `${inset.top}px`;
      layer.badge.style.right = `${inset.right}px`;
    }
  }

  // Draws the boxes and the badge into a layer's frame; returns the badge, if any.
  function fillLayer(frame, image, box, url, result) {
    frame.replaceChildren();

    if (showBoxes && result.object_detection && typeof result.object_detection === 'object') {
      const area = shared.pictureArea(box.width, box.height, image.naturalWidth, image.naturalHeight, box.fit, box.position);
      for (const [name, boxes] of Object.entries(result.object_detection)) {
        const color = shared.CLASS_COLORS[name] || '#000000';
        for (const coords of boxes) {
          const r = shared.boxRect(coords, area);
          const element = document.createElement('div');
          element.className = 'box';
          Object.assign(element.style, {
            left: `${r.left}px`, top: `${r.top}px`, width: `${r.width}px`, height: `${r.height}px`, borderColor: color,
          });
          const label = document.createElement('span');
          label.textContent = name;
          label.style.background = color;
          label.style.color = shared.labelTextColor(color);
          element.appendChild(label);
          frame.appendChild(element);
        }
      }
    }

    const info = shared.badge(result);
    if (!info) {
      return null;
    }
    const badge = document.createElement(info.kind === 'unknown' ? 'button' : 'span');
    badge.className = `badge ${info.band || info.kind}`;
    badge.textContent = info.text;
    badge.title = info.title;
    if (info.kind === 'unknown') {
      badge.type = 'button';
      // The layer sits inside the page's own elements, often a link: keep the page from
      // seeing presses on the badge.
      for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup']) {
        badge.addEventListener(type, (event) => event.stopPropagation());
      }
      badge.addEventListener('click', async (event) => {
        event.preventDefault();
        event.stopPropagation();
        badge.disabled = true;
        if (!(await analyse(url))) {
          badge.disabled = false;
        }
      });
    }
    frame.appendChild(badge);
    return badge;
  }

  function renderPanel() {
    const all = [...results.values()].filter((r) => r.status !== 'invalid');
    const answered = all.filter((r) => r.status !== 'looking');
    const known = all.filter((r) => r.status === 'done').length;
    const pending = all.filter(shared.isPending).length;
    const unknown = all.filter((r) => r.status === 'unknown').length;
    if (!all.length) {
      panel.counts.textContent = 'No images of at least 64 × 64 px on this page yet.';
    } else if (!answered.length) {
      panel.counts.textContent = `${all.length} images found`;
    } else {
      panel.counts.textContent =
        `${all.length} images · ${known} with results` +
        (pending ? ` · ${pending} being analysed` : '') +
        (unknown ? ` · ${unknown} not in Cortex` : '');
    }
    panel.message.textContent = message;
    panel.message.hidden = !message;
    panel.analyseAll.hidden = unknown === 0;
    panel.analyseAll.textContent = `Analyse ${unknown} new`;
    panel.toggleBoxes.textContent = showBoxes ? 'Hide boxes' : 'Show boxes';
  }

  function render() {
    if (closed) {
      return;
    }
    const drawn = new Set();
    for (const { image, rect } of candidates()) {
      const result = results.get(imageUrl(image));
      if (result && result.status !== 'looking' && result.status !== 'invalid') {
        drawLayer(image, rect, result);
        drawn.add(image);
      }
    }
    // An image that is gone, hidden, covered by another or showing a new picture not yet
    // looked up loses its layer, so nothing stale is left on the page.
    for (const image of [...layers.keys()]) {
      if (!drawn.has(image)) {
        removeLayer(image);
      }
    }
    renderPanel();
  }

  // --- wiring ------------------------------------------------------------------------------

  let frame = 0;
  const schedule = () => {
    if (!frame) {
      frame = requestAnimationFrame(() => {
        frame = 0;
        render();
      });
    }
  };
  let scanTimer = 0;
  const scheduleScan = () => {
    clearTimeout(scanTimer);
    scanTimer = setTimeout(scan, 400);
  };

  // Layers move with the page on their own; they are redrawn when an image changes size,
  // the window is resized, images load or the page's elements change. The periodic scan
  // catches what fires no event (a class that shows or moves an image).
  const resizes = new ResizeObserver(schedule);
  window.addEventListener('resize', schedule, { passive: true });
  document.addEventListener('load', scheduleScan, true); // images that finish loading later
  const isOurs = (node) => node === host || node.nodeName === LAYER_TAG.toUpperCase();
  const observer = new MutationObserver((records) => {
    if (records.some((r) => r.type === 'attributes' || ![...r.addedNodes, ...r.removedNodes].every(isOurs))) {
      scheduleScan();
    }
  });
  observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ['src', 'srcset'] });
  const refresh = setInterval(scan, 1000);
  const polling = setInterval(poll, POLL_MS);

  shadow.querySelector('.close').addEventListener('click', () => window.__cortexOverlay.close());
  panel.toggleBoxes.addEventListener('click', () => {
    showBoxes = !showBoxes;
    render();
  });
  panel.login.addEventListener('click', () => send({ type: 'open-app' }));
  panel.options.addEventListener('click', () => send({ type: 'open-options' }));
  panel.analyseAll.addEventListener('click', async () => {
    panel.analyseAll.disabled = true;
    const urls = [...results.entries()].filter(([, r]) => r.status === 'unknown').map(([url]) => url);
    for (const url of urls) {
      if (!(await analyse(url))) {
        break; // for example the rate limit: the message says so
      }
    }
    panel.analyseAll.disabled = false;
  });

  window.__cortexOverlay = {
    close() {
      closed = true;
      observer.disconnect();
      resizes.disconnect();
      clearInterval(refresh);
      clearInterval(polling);
      clearTimeout(scanTimer);
      cancelAnimationFrame(frame);
      window.removeEventListener('resize', schedule);
      document.removeEventListener('load', scheduleScan, true);
      for (const layer of layers.values()) {
        layer.element.remove();
      }
      layers.clear();
      host.remove();
      delete window.__cortexOverlay;
    },
  };

  scan();
})();
