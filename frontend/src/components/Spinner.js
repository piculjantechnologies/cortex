import React from 'react';
import './Spinner.css';

const Spinner = ({ label }) => (
  <div className="cortex-spinner" role="status" aria-label={label}>
    <div className="spinner" />
  </div>
);

export default Spinner;
