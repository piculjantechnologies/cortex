import React from 'react';
import { render, screen } from '@testing-library/react';
import Gallery from './Gallery';

const tile = (id) => ({
  _id: id,
  url: `https://images.example/${id}.jpg`,
  width: 100,
  height: 100,
  object_detection: { cat: [[0, 0, 1, 1]] },
  label_quality_score: 0.5,
});

// React warns about a missing list key only once per process, so this test lives in its
// own file (own module registry) and renders the gallery before anything else does.
test('renders several tiles without a React key warning', () => {
  render(<Gallery images={[tile('PT::1'), tile('PT::2'), tile('PT::3')]} />);

  // jest-fail-on-console fails the test if React logs the missing-key warning.
  expect(screen.getAllByRole('img', { name: 'Image with cat' })).toHaveLength(3);
  expect(screen.getAllByText('50.0%')).toHaveLength(3);
});

test('a tile without detections still gets alt text', () => {
  render(<Gallery images={[{ ...tile('PT::4'), object_detection: {} }]} />);

  expect(screen.getByRole('img', { name: 'Scraped image' })).toBeInTheDocument();
});
