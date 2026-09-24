import React from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { ROUTER_FUTURE } from './constants/router';
import './index.css';
import App from './App';

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <BrowserRouter future={ROUTER_FUTURE}>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
);
