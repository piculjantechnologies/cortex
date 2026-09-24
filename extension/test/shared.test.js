// Unit tests of lib/shared.js: node --test extension/test/*.test.js
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const shared = require('../lib/shared.js');

// Equal numbers up to floating-point noise (and -0 == 0), key by key.
function near(actual, expected) {
  assert.deepEqual(Object.keys(actual).sort(), Object.keys(expected).sort());
  for (const key of Object.keys(expected)) {
    assert.ok(Math.abs(actual[key] - expected[key]) < 1e-9, `${key}: ${actual[key]} != ${expected[key]}`);
  }
}

test('fill stretches the picture over the whole content box', () => {
  assert.deepEqual(shared.pictureArea(300, 100, 400, 200), { x: 0, y: 0, width: 300, height: 100 });
  assert.deepEqual(shared.pictureArea(300, 100, 400, 200, 'fill'), { x: 0, y: 0, width: 300, height: 100 });
});

test('contain letterboxes, centred by default', () => {
  // 400x200 picture in a 280x280 box: scale 0.7, 140 px high, 70 px above and below.
  assert.deepEqual(shared.pictureArea(280, 280, 400, 200, 'contain'), { x: 0, y: 70, width: 280, height: 140 });
});

test('cover crops, and object-position moves the crop', () => {
  // 400x200 in 100x100: scale 0.5, 200x100, 100 px cropped horizontally.
  assert.deepEqual(shared.pictureArea(100, 100, 400, 200, 'cover'), { x: -50, y: 0, width: 200, height: 100 });
  near(shared.pictureArea(100, 100, 400, 200, 'cover', '0% 50%'), { x: 0, y: 0, width: 200, height: 100 });
  assert.deepEqual(shared.pictureArea(100, 100, 400, 200, 'cover', 'right top'), { x: -100, y: 0, width: 200, height: 100 });
});

test('none and scale-down', () => {
  assert.deepEqual(shared.pictureArea(100, 100, 40, 20, 'none'), { x: 30, y: 40, width: 40, height: 20 });
  assert.deepEqual(shared.pictureArea(100, 100, 40, 20, 'scale-down'), { x: 30, y: 40, width: 40, height: 20 });
  assert.deepEqual(shared.pictureArea(100, 100, 400, 200, 'scale-down'), { x: 0, y: 25, width: 100, height: 50 });
});

test('a picture without a natural size falls back to the content box', () => {
  assert.deepEqual(shared.pictureArea(100, 50, 0, 0, 'contain'), { x: 0, y: 0, width: 100, height: 50 });
});

test('boxes map from normalised coordinates into the picture area', () => {
  const area = { x: 0, y: 70, width: 280, height: 140 };
  // The same numbers as the web app's gallery test: box [0.1, 0.2, 0.5, 0.6] on a 400x200 image in 280x280.
  near(shared.boxRect([0.1, 0.2, 0.5, 0.6], area), { left: 28, top: 98, width: 112, height: 56 });
});

test('stacked images share most of their area; a small image on a large one does not', () => {
  const rect = (left, top, width, height) => ({ left, top, right: left + width, bottom: top + height });
  assert.equal(shared.overlapRatio(rect(0, 0, 100, 100), rect(0, 0, 100, 100)), 1);
  // A search engine's preview: the full image (440x550) over its hidden, larger placeholder (527x660).
  assert.ok(shared.overlapRatio(rect(1305, 145, 440, 550), rect(1261, 90, 527, 660)) > 0.6);
  // A 100x100 logo on a 400x300 banner.
  assert.equal(shared.overlapRatio(rect(10, 10, 100, 100), rect(0, 0, 400, 300)), 10000 / 120000);
  assert.equal(shared.overlapRatio(rect(0, 0, 100, 100), rect(100, 0, 100, 100)), 0); // touching edges
  assert.equal(shared.overlapRatio(rect(0, 0, 0, 0), rect(0, 0, 0, 0)), 0);
});

test('the badge sits in the top right corner of the visible part of the image', () => {
  const box = { left: 100, top: 50, right: 659, bottom: 399 };
  assert.deepEqual(shared.badgeInset(box, box), { top: 6, right: 6 });
  // A container 16 px narrower on each side clips the image (a search engine's preview).
  assert.deepEqual(shared.badgeInset(box, { left: 116, top: 50, right: 643, bottom: 399 }), { top: 6, right: 22 });
  assert.deepEqual(shared.badgeInset(box, { left: 100, top: 80, right: 659, bottom: 399 }, 4), { top: 34, right: 4 });
});

test('badges follow the web app bands and every lookup state', () => {
  const done = { status: 'done', label_quality_score: 0.913, object_detection: { dog: [[0, 0, 1, 1]], person: [[0, 0, 1, 1], [0, 0, 1, 1]] } };
  assert.deepEqual(shared.badge(done), {
    kind: 'score', text: '91.3%', title: 'Label quality 91.3% · 2 person, 1 dog', band: 'good',
  });
  assert.equal(shared.badge({ ...done, label_quality_score: 0.6 }).band, 'fair');
  assert.equal(shared.badge({ ...done, label_quality_score: 0.2 }).band, 'poor');
  assert.equal(shared.badge({ status: 'done', label_quality_score: null, object_detection: {} }).kind, 'none');
  assert.equal(shared.badge({ status: 'queued' }).text, 'Queued…');
  assert.equal(shared.badge({ status: 'scoring' }).text, 'Scoring…');
  assert.match(shared.badge({ status: 'failed', reason: 'broken image' }).title, /broken image/);
  assert.equal(shared.badge({ status: 'unknown' }).kind, 'unknown');
  assert.equal(shared.badge({ status: 'invalid' }), null);
  assert.equal(shared.badge({ status: 'looking' }), null);
  assert.equal(shared.badge(undefined), null);
});

test('pending states are polled', () => {
  assert.ok(shared.isPending({ status: 'queued' }));
  assert.ok(shared.isPending({ status: 'scoring' }));
  assert.ok(!shared.isPending({ status: 'done' }));
  assert.ok(!shared.isPending(undefined));
});

test('settings URLs and host permission patterns', () => {
  assert.equal(shared.normaliseBase(' http://localhost:5000/api/ '), 'http://localhost:5000');
  assert.equal(shared.normaliseBase('https://cortex.example.com'), 'https://cortex.example.com');
  assert.equal(shared.normaliseBase('ftp://example.com'), null);
  assert.equal(shared.normaliseBase('not a url'), null);
  assert.equal(shared.originPattern('http://localhost:5000'), 'http://localhost/*');
  assert.equal(shared.originPattern('https://cortex.example.com'), 'https://cortex.example.com/*');
});

test('no deployment is built in; any http(s) origin can be granted on request', () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'manifest.json'), 'utf8'));
  assert.deepEqual(shared.DEFAULT_SETTINGS, { apiUrl: '', appUrl: '' });
  assert.equal(manifest.host_permissions, undefined);
  assert.deepEqual(manifest.optional_host_permissions, ['https://*/*', 'http://*/*']);
  assert.deepEqual(manifest.permissions, ['activeTab', 'scripting', 'storage']);
  for (const file of ['background.js', 'content.js', 'options.html', 'options.js', ...Object.values(manifest.icons)]) {
    assert.ok(fs.existsSync(path.join(__dirname, '..', file)), file);
  }
});

test('the class colours are the web app gallery colours', () => {
  const frontend = fs.readFileSync(path.join(__dirname, '..', '..', 'frontend', 'src', 'constants', 'vocClasses.js'), 'utf8');
  for (const [name, color] of Object.entries(shared.CLASS_COLORS)) {
    assert.ok(frontend.includes(`${name}: '${color}'`), name);
  }
  assert.equal(Object.keys(shared.CLASS_COLORS).length, 20);
  assert.equal(shared.labelTextColor('#fab005'), '#000');
  assert.equal(shared.labelTextColor('#364fc7'), '#fff');
});

test('messages for failed API calls', () => {
  assert.match(shared.apiErrorText(401), /Log in/);
  assert.match(shared.apiErrorText(403), /subscription/);
  assert.match(shared.apiErrorText(429), /Too many/);
  assert.equal(shared.apiErrorText(0, 'offline'), 'offline');
  assert.equal(shared.apiErrorText(500, 'boom'), 'boom');
  assert.equal(shared.apiErrorText(502), 'Cortex answered 502.');
  assert.deepEqual(shared.chunk([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]]);
});
