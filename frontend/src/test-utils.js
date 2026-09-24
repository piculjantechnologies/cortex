// Shared test harness. Test files that use mockApi() must mock the client first:
//
//   jest.mock('./api/client', () => ({ ...jest.requireActual('./api/client'), apiFetch: jest.fn() }));
//
// CRA runs every test with resetMocks, so arm apiFetch again in each test (or beforeEach).
import React, { useState } from 'react';
import { render } from '@testing-library/react';
import { GoogleOAuthProvider } from '@react-oauth/google';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { AuthContext } from './context/AuthContext';
import { ApiError, apiFetch } from './api/client';
import { ROUTER_FUTURE } from './constants/router';

// Stand-in pages, so a redirect can be asserted by the heading it lands on.
export const SENTINEL_ROUTES = {
  '/login': 'Login page',
  '/dashboard': 'Dashboard page',
};

function TestAuthProvider({ initial, children }) {
  const [isAuthenticated, setIsAuthenticated] = useState(initial);
  return (
    <AuthContext.Provider value={{ isAuthenticated, setIsAuthenticated, checking: false }}>
      {children}
    </AuthContext.Provider>
  );
}

/**
 * Renders `ui` at `route` (a path or a location object) inside GoogleOAuthProvider, a
 * stateful AuthContext and a MemoryRouter. `path` is the route pattern `ui` is mounted
 * on (defaults to the route's pathname); `routes` maps other paths to sentinel headings.
 */
export function renderWithProviders(
  ui,
  { route = '/', path, auth = {}, routes = SENTINEL_ROUTES } = {},
) {
  const pagePath = path || (typeof route === 'string' ? route : route.pathname).split('?')[0];
  const sentinels = Object.entries(routes).filter(([sentinelPath]) => sentinelPath !== pagePath);
  return render(
    <GoogleOAuthProvider clientId="test">
      <TestAuthProvider initial={Boolean(auth.isAuthenticated)}>
        <MemoryRouter initialEntries={[route]} future={ROUTER_FUTURE}>
          <Routes>
            <Route path={pagePath} element={ui} />
            {sentinels.map(([sentinelPath, heading]) => (
              <Route key={sentinelPath} path={sentinelPath} element={<h1>{heading}</h1>} />
            ))}
          </Routes>
        </MemoryRouter>
      </TestAuthProvider>
    </GoogleOAuthProvider>,
  );
}

/**
 * Routes the mocked apiFetch by path. Each handler is a value to resolve with, an Error
 * to reject with, or a function (options, callNumber) returning either.
 */
export function mockApi(handlers) {
  const calls = {};
  apiFetch.mockImplementation((requestPath, options = {}) => {
    if (!(requestPath in handlers)) {
      return Promise.reject(new Error(`Unexpected request to ${requestPath}`));
    }
    calls[requestPath] = (calls[requestPath] || 0) + 1;
    let result = handlers[requestPath];
    if (typeof result === 'function') {
      result = result(options, calls[requestPath]);
    }
    return result instanceof Error ? Promise.reject(result) : Promise.resolve(result);
  });
}

export const apiError = (status, message) => new ApiError(message, status, { message });

// The [path, options] pairs apiFetch was called with.
export const apiCalls = () => apiFetch.mock.calls;
