// Helpers shared by the service worker, the page script and the options page, with no
// browser APIs, so they are unit-tested with Node (see test/shared.test.js).
(function (root) {
  'use strict';

  // The box colours of the web app's gallery (frontend/src/constants/vocClasses.js).
  const CLASS_COLORS = {
    aeroplane: '#e03131',
    bicycle: '#1c7ed6',
    bird: '#2f9e44',
    boat: '#7048e8',
    bottle: '#f76707',
    bus: '#0c8599',
    car: '#e64980',
    cat: '#fab005',
    chair: '#8d6e63',
    cow: '#15aabf',
    diningtable: '#ae3ec9',
    dog: '#82c91e',
    horse: '#a61e4d',
    motorbike: '#364fc7',
    person: '#12b886',
    pottedplant: '#5c940d',
    sheep: '#ffa8a8',
    sofa: '#b197fc',
    train: '#495057',
    tvmonitor: '#66d9e8',
  };

  // No Cortex deployment is built in: the user enters theirs in the options.
  const DEFAULT_SETTINGS = { apiUrl: '', appUrl: '' };

  // Black or white, whichever reads better on the given #rrggbb background.
  function labelTextColor(hex) {
    const value = /^#([0-9a-f]{6})$/i.exec(hex || '');
    if (!value) {
      return '#000';
    }
    const n = parseInt(value[1], 16);
    const luminance = (0.299 * (n >> 16) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255)) / 255;
    return luminance > 0.6 ? '#000' : '#fff';
  }

  // The web app's badge bands: good from 80 %, fair from 50 %, poor below.
  function qualityBand(score) {
    if (score >= 0.8) {
      return 'good';
    }
    return score >= 0.5 ? 'fair' : 'poor';
  }

  // "https://host:port" for an http(s) URL typed in the options, or null.
  function normaliseBase(text) {
    try {
      const url = new URL(String(text).trim());
      if (url.protocol !== 'http:' && url.protocol !== 'https:') {
        return null;
      }
      return url.origin;
    } catch {
      return null;
    }
  }

  // The host permission pattern that covers a base URL (match patterns ignore the port).
  function originPattern(base) {
    const url = new URL(base);
    return `${url.protocol}//${url.hostname}/*`;
  }

  function parsePercent(token, fallback) {
    const match = /^(-?\d+(?:\.\d+)?)%$/.exec(token || '');
    if (match) {
      return Number(match[1]) / 100;
    }
    return { left: 0, top: 0, center: 0.5, right: 1, bottom: 1 }[token] ?? fallback;
  }

  // Where the picture itself is drawn inside an <img>'s content box, for its object-fit and
  // object-position: {x, y, width, height} relative to the content box.
  function pictureArea(boxWidth, boxHeight, naturalWidth, naturalHeight, objectFit = 'fill', objectPosition = '50% 50%') {
    if (!naturalWidth || !naturalHeight || objectFit === 'fill' || !objectFit) {
      return { x: 0, y: 0, width: boxWidth, height: boxHeight };
    }
    const containScale = Math.min(boxWidth / naturalWidth, boxHeight / naturalHeight);
    const coverScale = Math.max(boxWidth / naturalWidth, boxHeight / naturalHeight);
    let scale;
    if (objectFit === 'contain') {
      scale = containScale;
    } else if (objectFit === 'cover') {
      scale = coverScale;
    } else if (objectFit === 'none') {
      scale = 1;
    } else if (objectFit === 'scale-down') {
      scale = Math.min(1, containScale);
    } else {
      return { x: 0, y: 0, width: boxWidth, height: boxHeight };
    }
    const width = naturalWidth * scale;
    const height = naturalHeight * scale;
    const [px, py] = String(objectPosition).trim().split(/\s+/);
    const fx = parsePercent(px, 0.5);
    const fy = parsePercent(py === undefined ? 'center' : py, 0.5);
    return { x: (boxWidth - width) * fx, y: (boxHeight - height) * fy, width, height };
  }

  // A stored box ([x1, y1, x2, y2] normalised to the image) in pixels of the picture area.
  function boxRect(box, area) {
    const [x1, y1, x2, y2] = box;
    return {
      left: area.x + x1 * area.width,
      top: area.y + y1 * area.height,
      width: (x2 - x1) * area.width,
      height: (y2 - y1) * area.height,
    };
  }

  // The share of the larger of two rectangles ({left, top, right, bottom}) that they have
  // in common: 1 for the same rectangle, small for a small one inside a large one, 0 apart.
  function overlapRatio(a, b) {
    const width = Math.min(a.right, b.right) - Math.max(a.left, b.left);
    const height = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
    const larger = Math.max((a.right - a.left) * (a.bottom - a.top), (b.right - b.left) * (b.bottom - b.top));
    return width > 0 && height > 0 && larger > 0 ? (width * height) / larger : 0;
  }

  // Where the badge goes, as distances from the top and right of the image's content box
  // ({left, top, right, bottom}): a margin inside the top right corner of the part of the
  // box that is visible, when an ancestor clips the image.
  function badgeInset(box, visible, margin = 6) {
    return {
      top: Math.max(0, visible.top - box.top) + margin,
      right: Math.max(0, box.right - visible.right) + margin,
    };
  }

  // "2 person, 1 dog" for a {class: boxes} map, largest count first.
  function summarise(objectDetection) {
    return Object.entries(objectDetection || {})
      .filter(([, boxes]) => Array.isArray(boxes) && boxes.length > 0)
      .sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]))
      .map(([name, boxes]) => `${boxes.length} ${name}`)
      .join(', ');
  }

  const PENDING = new Set(['queued', 'scoring']);

  // How a lookup result is shown: {kind, text, title, band}.
  function badge(result) {
    const status = result && result.status;
    if (status === 'done') {
      const objects = summarise(result.object_detection);
      if (typeof result.label_quality_score === 'number') {
        const percent = (result.label_quality_score * 100).toFixed(1);
        return {
          kind: 'score',
          text: `${percent}%`,
          title: `Label quality ${percent}%${objects ? ` · ${objects}` : ''}`,
          band: qualityBand(result.label_quality_score),
        };
      }
      return { kind: 'none', text: 'No objects', title: 'No Pascal VOC objects were detected.' };
    }
    if (PENDING.has(status)) {
      return {
        kind: 'pending',
        text: status === 'queued' ? 'Queued…' : 'Scoring…',
        title: status === 'queued' ? 'Waiting for object detection' : 'Objects detected; scoring the labels',
      };
    }
    if (status === 'failed') {
      return { kind: 'failed', text: 'Cannot analyse', title: `Cortex could not analyse this image (${result.reason}).` };
    }
    if (status === 'unknown') {
      return { kind: 'unknown', text: 'Analyse', title: 'Not in Cortex yet: queue it for detection and scoring.' };
    }
    return null;
  }

  function isPending(result) {
    return Boolean(result) && PENDING.has(result.status);
  }

  function chunk(items, size) {
    const out = [];
    for (let i = 0; i < items.length; i += size) {
      out.push(items.slice(i, i + size));
    }
    return out;
  }

  // The message shown for a failed API call.
  function apiErrorText(status, message) {
    if (status === 401) {
      return 'Log in to Cortex to see its results.';
    }
    if (status === 403) {
      return 'An active Cortex subscription is required.';
    }
    if (status === 429) {
      return 'Too many requests; try again in a minute.';
    }
    if (status === 0) {
      return message || 'Cortex could not be reached.';
    }
    return message || `Cortex answered ${status}.`;
  }

  const api = {
    CLASS_COLORS,
    DEFAULT_SETTINGS,
    apiErrorText,
    badge,
    badgeInset,
    boxRect,
    chunk,
    isPending,
    labelTextColor,
    normaliseBase,
    originPattern,
    overlapRatio,
    pictureArea,
    qualityBand,
    summarise,
  };
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.CortexShared = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this);
