// jest-dom adds custom jest matchers for asserting on DOM nodes,
// e.g. expect(element).toHaveTextContent(/react/i).
import '@testing-library/jest-dom';
import failOnConsole from 'jest-fail-on-console';

// Any console.error or console.warn (React key or act() warnings, router warnings,
// jsdom "not implemented" errors) fails the test that caused it.
failOnConsole();
