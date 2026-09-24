import React from 'react';
import { CLASS_COLORS, VOC_CLASSES } from '../constants/vocClasses';
import { buildFilter, FILTER_EXAMPLES, formatFilter, parseFilterText, SORT_OPTIONS } from '../api/query';

const STATE_LABELS = { included: 'included', excluded: 'excluded' };

// A number field with its unit inside the right edge.
function NumberField({ id, label, value, onChange, unit, placeholder, min = 0, max, disabled }) {
  return (
    <div className="cortex-field">
      <label htmlFor={id}>{label}</label>
      <div className="cortex-input-unit">
        <input
          id={id}
          className="cortex-input"
          type="number"
          min={min}
          max={max}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          disabled={disabled}
        />
        <span aria-hidden="true">{unit}</span>
      </div>
    </div>
  );
}

function ClassFilters({ filters, onChange }) {
  const { includeClasses, excludeClasses, includeMode, onlyIncluded } = filters;
  const hasIncluded = includeClasses.length > 0;

  const classState = (name) => {
    if (includeClasses.includes(name)) {
      return 'included';
    }
    return excludeClasses.includes(name) ? 'excluded' : 'neutral';
  };

  // A click cycles a class: any -> included -> excluded -> any.
  const cycleClass = (name) => {
    const state = classState(name);
    const without = (list) => list.filter((value) => value !== name);
    if (state === 'neutral') {
      onChange({ includeClasses: [...includeClasses, name] });
    } else if (state === 'included') {
      onChange({ includeClasses: without(includeClasses), excludeClasses: [...excludeClasses, name] });
    } else {
      onChange({ excludeClasses: without(excludeClasses) });
    }
  };

  return (
    <div className="cortex-classes" role="group" aria-labelledby="classes-label">
      <div className="cortex-classes-head">
        <span id="classes-label" className="cortex-classes-label">
          Classes
        </span>
        <span className="cortex-hint">Click to include, again to exclude, again to clear</span>
        <div className="cortex-classes-tools">
          <div className="cortex-segmented" role="radiogroup" aria-label="Included classes must">
            {[
              ['all', 'Match all'],
              ['any', 'Match any'],
            ].map(([value, label]) => (
              <label key={value} className={includeMode === value ? 'is-active' : undefined}>
                <input
                  type="radio"
                  name="include-mode"
                  value={value}
                  checked={includeMode === value}
                  onChange={() => onChange({ includeMode: value })}
                />
                {label}
              </label>
            ))}
          </div>
          <button
            type="button"
            className="cortex-link-button"
            onClick={() => onChange({ includeClasses: [], excludeClasses: [], onlyIncluded: false })}
            disabled={!hasIncluded && excludeClasses.length === 0}
          >
            Clear
          </button>
        </div>
      </div>
      <div className="cortex-chips">
        {VOC_CLASSES.map((name) => {
          const state = classState(name);
          return (
            <button
              key={name}
              type="button"
              className={`cortex-chip is-${state}`}
              onClick={() => cycleClass(name)}
              aria-label={state === 'neutral' ? name : `${name}, ${STATE_LABELS[state]}`}
            >
              <span className="cortex-chip-mark" aria-hidden="true">
                {state === 'included' && '✓'}
                {state === 'excluded' && '✕'}
              </span>
              <span className="cortex-class-dot" style={{ backgroundColor: CLASS_COLORS[name] }} aria-hidden="true" />
              {name}
            </button>
          );
        })}
      </div>
      <div className={`cortex-class-options${hasIncluded ? '' : ' is-disabled'}`}>
        <span className="cortex-hint">Each included class:</span>
        <NumberField
          id="min-count"
          label="Min objects"
          value={filters.minCount}
          onChange={(value) => onChange({ minCount: value })}
          unit="×"
          placeholder="1"
          min={1}
          disabled={!hasIncluded}
        />
        <NumberField
          id="min-box-area"
          label="Largest box at least"
          value={filters.minBoxArea}
          onChange={(value) => onChange({ minBoxArea: value })}
          unit="% of image"
          placeholder="0"
          max={100}
          disabled={!hasIncluded}
        />
        <label className="cortex-check">
          <input
            type="checkbox"
            checked={onlyIncluded}
            onChange={(e) => onChange({ onlyIncluded: e.target.checked })}
            disabled={!hasIncluded}
          />
          Only these classes (no other objects)
        </label>
      </div>
    </div>
  );
}

function AdvancedFilter({ filters, onChange }) {
  let status;
  try {
    parseFilterText(filters.advancedText);
    status = { ok: true, text: 'Valid JSON' };
  } catch (err) {
    status = { ok: false, text: err.message };
  }

  return (
    <div className="cortex-advanced">
      <div className="cortex-advanced-tools" role="group" aria-labelledby="start-from-label">
        <span id="start-from-label" className="cortex-hint">
          Start from:
        </span>
        <button
          type="button"
          className="cortex-preset"
          onClick={() => onChange({ advancedText: formatFilter(buildFilter(filters)) })}
        >
          The Filters tab
        </button>
        {FILTER_EXAMPLES.map((example) => (
          <button
            key={example.label}
            type="button"
            className="cortex-preset"
            onClick={() => onChange({ advancedText: formatFilter(example.filter) })}
          >
            {example.label}
          </button>
        ))}
      </div>
      <label htmlFor="advanced-filter" className="cortex-visually-hidden">
        Filter (JSON)
      </label>
      <textarea
        id="advanced-filter"
        className="cortex-input cortex-code"
        spellCheck={false}
        rows={14}
        value={filters.advancedText}
        onChange={(e) => onChange({ advancedText: e.target.value })}
        placeholder='{"$and": [{"class": "person", "count": {"$gte": 2}}, {"label_quality": {"$gte": 0.8}}]}'
      />
      <p className={`cortex-json-status ${status.ok ? 'is-ok' : 'is-error'}`} aria-live="polite">
        {status.text}
      </p>
      <details className="cortex-reference">
        <summary>Filter reference</summary>
        <dl>
          <dt>
            <code>{'{"class": "dog", "count": …, "max_box_area": …}'}</code>
          </dt>
          <dd>
            The image contains a dog; optionally a box count (integer ≥ 1) and the area of the largest dog box
            as a fraction of the image (0–1).
          </dd>
          <dt>
            <code>width</code>, <code>height</code>, <code>label_quality</code>, <code>object_count</code>,{' '}
            <code>collected</code>
          </dt>
          <dd>
            Pixels; label-quality score (0–1); boxes of all classes; collection time as an ISO date such as{' '}
            <code>&quot;2026-09-01&quot;</code> (only <code>$gt</code>, <code>$gte</code>, <code>$lt</code>,{' '}
            <code>$lte</code>).
          </dd>
          <dt>
            <code>$eq</code> <code>$gt</code> <code>$gte</code> <code>$lt</code> <code>$lte</code> <code>$in</code>
          </dt>
          <dd>
            Comparisons, e.g. <code>{'{"width": {"$gte": 640, "$lt": 2000}}'}</code>; a bare value means{' '}
            <code>$eq</code>.
          </dd>
          <dt>
            <code>$and</code> <code>$or</code> <code>$not</code>
          </dt>
          <dd>
            Combine conditions: <code>{'{"$and": [...]}'}</code>, <code>{'{"$or": [...]}'}</code>,{' '}
            <code>{'{"$not": {...}}'}</code>. At most 8 levels deep and 64 conditions.
          </dd>
        </dl>
      </details>
    </div>
  );
}

// The search filters. `filters` holds the raw form values; the parent turns them into
// the API request with buildSearch() when the form is submitted.
const SearchForm = ({ filters, onChange, onSubmit, onDownload, canDownload }) => {
  const advanced = filters.mode === 'advanced';

  // The slider follows the number field; an empty or invalid field leaves it at 0.
  const qualityValue = Number(filters.labelQuality);
  const sliderValue = Number.isFinite(qualityValue) ? Math.min(Math.max(qualityValue, 0), 100) : 0;

  const switchMode = (mode) => {
    // The advanced editor starts from the simple filters the first time it opens.
    if (mode === 'advanced' && filters.advancedText.trim() === '') {
      onChange({ mode, advancedText: formatFilter(buildFilter(filters)) });
    } else {
      onChange({ mode });
    }
  };

  return (
    <form className="cortex-filters" onSubmit={onSubmit}>
      <div className="cortex-filters-head">
        <div className="cortex-tabs" role="tablist" aria-label="Filter mode">
          {[
            ['simple', 'Filters'],
            ['advanced', 'Advanced'],
          ].map(([mode, label]) => (
            <button
              key={mode}
              type="button"
              role="tab"
              aria-selected={filters.mode === mode}
              className={filters.mode === mode ? 'is-active' : undefined}
              onClick={() => switchMode(mode)}
            >
              {label}
            </button>
          ))}
        </div>
        <ul className="cortex-scope" aria-label="Scope">
          <li>Image</li>
          <li>Object detection</li>
          <li>Pascal VOC, 20 classes</li>
        </ul>
      </div>

      {advanced ? (
        <AdvancedFilter filters={filters} onChange={onChange} />
      ) : (
        <>
          <ClassFilters filters={filters} onChange={onChange} />

          <div className="cortex-filters-grid">
            <NumberField
              id="min-width"
              label="Min width"
              value={filters.minWidth}
              onChange={(value) => onChange({ minWidth: value })}
              unit="px"
              placeholder="100"
            />
            <NumberField
              id="min-height"
              label="Min height"
              value={filters.minHeight}
              onChange={(value) => onChange({ minHeight: value })}
              unit="px"
              placeholder="0"
            />

            <div className="cortex-field cortex-field-wide">
              <label htmlFor="label-quality">Min label quality</label>
              <div className="cortex-quality">
                <input
                  type="range"
                  className="cortex-range"
                  min={0}
                  max={100}
                  step={1}
                  value={sliderValue}
                  onChange={(e) => onChange({ labelQuality: e.target.value })}
                  aria-label="Min label quality slider"
                  style={{ '--cx-range-fill': `${sliderValue}%` }}
                />
                <div className="cortex-input-unit cortex-quality-number">
                  <input
                    id="label-quality"
                    className="cortex-input"
                    type="number"
                    min={0}
                    max={100}
                    value={filters.labelQuality}
                    onChange={(e) => onChange({ labelQuality: e.target.value })}
                    placeholder="50"
                  />
                  <span aria-hidden="true">%</span>
                </div>
              </div>
            </div>
          </div>
        </>
      )}

      <div className="cortex-filters-actions">
        <div className="cortex-sort">
          <label htmlFor="sort">Sort by</label>
          <select
            id="sort"
            className="cortex-input"
            value={filters.sort}
            onChange={(e) => onChange({ sort: e.target.value })}
          >
            {SORT_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
        <button className="cortex-button cortex-button-secondary" type="button" onClick={onDownload} disabled={!canDownload}>
          Download CSV
        </button>
        <button className="cortex-button" type="submit">
          Search
        </button>
      </div>
    </form>
  );
};

export default SearchForm;
