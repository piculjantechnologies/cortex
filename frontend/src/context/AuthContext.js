import React, { createContext, useEffect, useMemo, useState } from 'react';
import { apiFetch, onUnauthorized } from '../api/client';

// { isAuthenticated, setIsAuthenticated, checking }
export const AuthContext = createContext({
  isAuthenticated: false,
  setIsAuthenticated: () => {},
  checking: false,
});

// Holds the login state for the whole app. The session itself lives in the backend's
// cookies, so on start-up the provider asks GET /api/check-auth instead of trusting any
// client-side flag, and any later 401 from the backend marks the user as logged out.
export function AuthProvider({ children }) {
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [checking, setChecking] = useState(true);

  useEffect(() => onUnauthorized(() => setIsAuthenticated(false)), []);

  useEffect(() => {
    const controller = new AbortController();
    const checkAuth = async () => {
      let authenticated = false;
      try {
        const data = await apiFetch('/api/check-auth', { signal: controller.signal });
        authenticated = Boolean(data && data.authenticated);
      } catch {
        // 401 (anonymous) or the backend is unreachable: treat both as logged out.
      }
      if (!controller.signal.aborted) {
        setIsAuthenticated(authenticated);
        setChecking(false);
      }
    };
    checkAuth();
    return () => controller.abort();
  }, []);

  const value = useMemo(
    () => ({ isAuthenticated, setIsAuthenticated, checking }),
    [isAuthenticated, checking],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
