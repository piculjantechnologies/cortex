// The /api/get-labeled-data request, built in one place for search, pagination and CSV.
// Both form modes send a `filter` in the backend's filter language (see backend/docs/API.md).
import { VOC_CLASSES } from '../constants/vocClasses';

// The form's defaults: min width 100 px, min height 0, label quality 50 %.
const DEFAULTS = { minWidth: 100, minHeight: 0, labelQuality: 50 };

// Result orders offered in the form (the API's `sort` values).
export const SORT_OPTIONS = [
  { value: 'quality', label: 'Best label quality' },
  { value: 'newest', label: 'Newest' },
  { value: 'id', label: 'Oldest' },
];

// Initial form state. Numbers are kept as the strings the inputs hold. includeMode
// says whether an image needs every included class ('all') or at least one ('any');
// minCount and minBoxArea (percent of the image) apply to each included class.
export const DEFAULT_FILTERS = {
  mode: 'simple',
  includeClasses: [],
  excludeClasses: [],
  includeMode: 'all',
  onlyIncluded: false,
  minCount: '',
  minBoxArea: '',
  minWidth: String(DEFAULTS.minWidth),
  minHeight: String(DEFAULTS.minHeight),
  labelQuality: String(DEFAULTS.labelQuality),
  advancedText: '',
  sort: 'quality',
};

const anyOf = (names) => ({ $or: names.map((name) => ({ class: name })) });

// Ready-made filters for the advanced editor.
export const FILTER_EXAMPLES = [
  {
    label: 'Two or more people and nothing else',
    filter: {
      $and: [
        { class: 'person', count: { $gte: 2 } },
        { $not: anyOf(VOC_CLASSES.filter((name) => name !== 'person')) },
      ],
    },
  },
  {
    label: 'A large cat, together with a dog or with no other animal',
    filter: {
      $and: [
        { class: 'cat', max_box_area: { $gte: 0.2 } },
        { $or: [{ class: 'dog' }, { $not: anyOf(['bird', 'cow', 'horse', 'sheep']) }] },
      ],
    },
  },
  {
    label: 'Vehicles in large, well-labelled images collected since 1 Sep 2026',
    filter: {
      $and: [
        anyOf(['bus', 'car', 'motorbike', 'train']),
        { width: { $gte: 1024 }, height: { $gte: 768 } },
        { label_quality: { $gte: 0.8 } },
        { collected: { $gte: '2026-09-01' } },
      ],
    },
  },
];

function toNumber(value, fallback) {
  const number = String(value).trim() === '' ? NaN : Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function combine(conditions) {
  if (conditions.length === 0) {
    return {};
  }
  return conditions.length === 1 ? conditions[0] : { $and: conditions };
}

// The simple form as a filter: the same JSON the advanced editor starts from.
export function buildFilter(filters) {
  const { includeClasses, excludeClasses } = filters;
  const conditions = [];

  const minCount = Math.floor(toNumber(filters.minCount, 1));
  const minBoxArea = toNumber(filters.minBoxArea, 0);
  const included = includeClasses.map((name) => {
    const condition = { class: name };
    if (minCount > 1) {
      condition.count = { $gte: minCount };
    }
    if (minBoxArea > 0) {
      condition.max_box_area = { $gte: Math.min(minBoxArea, 100) / 100 };
    }
    return condition;
  });
  if (included.length > 1 && filters.includeMode === 'any') {
    conditions.push({ $or: included });
  } else {
    conditions.push(...included);
  }

  // "Only these classes" excludes every class that is not included.
  const excluded =
    filters.onlyIncluded && includeClasses.length > 0
      ? VOC_CLASSES.filter((name) => !includeClasses.includes(name))
      : excludeClasses;
  if (excluded.length > 0) {
    conditions.push({ $not: excluded.length === 1 ? { class: excluded[0] } : anyOf(excluded) });
  }

  const minWidth = toNumber(filters.minWidth, DEFAULTS.minWidth);
  const minHeight = toNumber(filters.minHeight, DEFAULTS.minHeight);
  const labelQuality = toNumber(filters.labelQuality, DEFAULTS.labelQuality);
  if (minWidth > 0) {
    conditions.push({ width: { $gte: minWidth } });
  }
  if (minHeight > 0) {
    conditions.push({ height: { $gte: minHeight } });
  }
  if (labelQuality > 0) {
    conditions.push({ label_quality: { $gte: Math.min(labelQuality, 100) / 100 } });
  }
  return combine(conditions);
}

export const formatFilter = (filter) => JSON.stringify(filter, null, 2);

// Parses the advanced editor's text; throws an Error with a readable message.
export function parseFilterText(text) {
  if (text.trim() === '') {
    return {};
  }
  let filter;
  try {
    filter = JSON.parse(text);
  } catch (err) {
    throw new Error(`The filter is not valid JSON: ${err.message}`);
  }
  if (filter === null || typeof filter !== 'object' || Array.isArray(filter)) {
    throw new Error('The filter must be a JSON object.');
  }
  return filter;
}

// The request body without page and fetch_all: {sort, filter}. Throws for invalid advanced text.
export function buildSearch(filters) {
  const filter = filters.mode === 'advanced' ? parseFilterText(filters.advancedText) : buildFilter(filters);
  return { sort: filters.sort, filter };
}
