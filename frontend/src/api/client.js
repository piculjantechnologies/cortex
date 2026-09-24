// Single entry point for every call to the Flask backend.
//
// REACT_APP_API_URL is empty in production (the SPA and /api/ share one origin behind
// the reverse proxy) and http://localhost:5000 in local development.
const API_URL = process.env.REACT_APP_API_URL || '';

export const NETWORK_ERROR_MESSAGE =
  'Could not reach the server. Check your connection and try again.';

// The backend's 429 message is the raw limit ("10 per 1 minute"), so it is not shown.
export const RATE_LIMIT_MESSAGE = 'Too many attempts. Please wait a moment and try again.';

export class ApiError extends Error {
  constructor(message, status, data = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.data = data;
  }
}

let unauthorizedHandler = null;

// Registers the callback that runs whenever the backend answers 401 (the session is
// gone). Returns a function that unregisters it, so it can be used as an effect cleanup.
export function onUnauthorized(handler) {
  unauthorizedHandler = handler;
  return () => {
    if (unauthorizedHandler === handler) {
      unauthorizedHandler = null;
    }
  };
}

// The message to show for an error thrown by apiFetch, or `fallback` for anything else.
export function errorMessage(error, fallback) {
  return error instanceof ApiError && error.message ? error.message : fallback;
}

async function readBody(response) {
  const text = await response.text();
  if (!text) {
    return null;
  }
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

/**
 * Calls the backend and returns the parsed JSON body (or the text when it is not JSON).
 *
 * - `body` is sent as JSON; a request without `body` sends no body at all.
 * - `credentials` defaults to 'include' so the session cookie travels; pass 'omit' for
 *   the unauthenticated flows (register, forgot and reset password).
 * - `raw: true` returns the Response itself once it is known to be OK (CSV downloads).
 * - A non-2xx answer throws ApiError(message, status, data), using the backend's
 *   `message` or `error` field when there is one (a 429 always gets RATE_LIMIT_MESSAGE);
 *   a 401 also runs the onUnauthorized handler. A network failure throws ApiError with
 *   status 0; an abort is rethrown as is.
 */
export async function apiFetch(
  path,
  { method = 'GET', body, credentials = 'include', signal, raw = false } = {},
) {
  const init = { method, credentials, signal };
  if (body !== undefined) {
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(body);
  }

  let response;
  try {
    response = await fetch(`${API_URL}${path}`, init);
  } catch (error) {
    if (error && error.name === 'AbortError') {
      throw error;
    }
    throw new ApiError(NETWORK_ERROR_MESSAGE, 0);
  }

  if (response.ok && raw) {
    return response;
  }

  const data = await readBody(response);
  if (!response.ok) {
    if (response.status === 401 && unauthorizedHandler) {
      unauthorizedHandler();
    }
    const serverMessage = data && typeof data === 'object' ? data.message || data.error : null;
    let message = `Request failed (HTTP ${response.status}).`;
    if (response.status === 429) {
      message = RATE_LIMIT_MESSAGE;
    } else if (typeof serverMessage === 'string' && serverMessage) {
      message = serverMessage;
    }
    throw new ApiError(message, response.status, data);
  }
  return data;
}
