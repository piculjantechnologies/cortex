import React from 'react';
import './Footer.css';

const PLATFORM_URL = 'https://www.piculjantechnologies.ai/cortex-platform';

const Footer = () => (
  <footer className="cortex-footer">
    <a
      href={PLATFORM_URL}
      target="_blank"
      rel="noopener noreferrer"
      className="cortex-footer-link"
    >
      {PLATFORM_URL.replace(/^https:\/\/(www\.)?/, '')}
    </a>
    <p>Copyright © {new Date().getFullYear()} Neven Pičuljan</p>
  </footer>
);

export default Footer;
