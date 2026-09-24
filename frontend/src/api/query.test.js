import { buildFilter, buildSearch, DEFAULT_FILTERS, FILTER_EXAMPLES, parseFilterText } from './query';

const form = (patch) => ({ ...DEFAULT_FILTERS, ...patch });

test('an emptied number falls back to its default; zero minimums add no condition', () => {
  expect(buildFilter(form({ minWidth: '', minHeight: '', labelQuality: '0' }))).toEqual({ width: { $gte: 100 } });
  expect(buildFilter(form({ minWidth: '0', minHeight: '0', labelQuality: '0' }))).toEqual({});
});

test('Match any puts the included classes, with their count and size, under $or', () => {
  const filter = buildFilter(
    form({ includeClasses: ['dog', 'cat'], includeMode: 'any', minCount: '2', minBoxArea: '150', minWidth: '0', labelQuality: '0' }),
  );
  expect(filter).toEqual({
    $or: [
      { class: 'dog', count: { $gte: 2 }, max_box_area: { $gte: 1 } },
      { class: 'cat', count: { $gte: 2 }, max_box_area: { $gte: 1 } },
    ],
  });
});

test('a single class under Match any needs no $or', () => {
  expect(buildFilter(form({ includeClasses: ['dog'], includeMode: 'any', minWidth: '0', labelQuality: '0' }))).toEqual({
    class: 'dog',
  });
});

test('"Only these classes" excludes every other class and overrides the excluded chips', () => {
  const filter = buildFilter(
    form({ includeClasses: ['dog'], excludeClasses: ['cat'], onlyIncluded: true, minWidth: '0', labelQuality: '0' }),
  );
  expect(filter.$and[0]).toEqual({ class: 'dog' });
  expect(filter.$and[1].$not.$or).toHaveLength(19);
});

test('"Only these classes" without an included class does nothing', () => {
  expect(buildFilter(form({ onlyIncluded: true, minWidth: '0', labelQuality: '0' }))).toEqual({});
});

test('advanced text is parsed; errors are readable', () => {
  expect(parseFilterText('  ')).toEqual({});
  expect(buildSearch(form({ mode: 'advanced', advancedText: '{"class": "dog"}', sort: 'id' }))).toEqual({
    sort: 'id',
    filter: { class: 'dog' },
  });
  expect(() => parseFilterText('{"class":')).toThrow(/not valid JSON/);
  expect(() => parseFilterText('[1]')).toThrow('The filter must be a JSON object.');
});

test('the examples stay within the 64-condition limit', () => {
  // Every non-operator key of a condition object counts once, like the backend's class/field conditions.
  const count = (node) =>
    Array.isArray(node)
      ? node.reduce((sum, item) => sum + count(item), 0)
      : Object.entries(node).reduce((sum, [key, value]) => sum + (key.startsWith('$') ? count(value) : 1), 0);
  for (const example of FILTER_EXAMPLES) {
    expect(count(example.filter)).toBeLessThanOrEqual(64);
  }
});
