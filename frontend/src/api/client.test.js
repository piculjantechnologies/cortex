import {
  ApiError,
  apiFetch,
  errorMessage,
  NETWORK_ERROR_MESSAGE,
  onUnauthorized,
  RATE_LIMIT_MESSAGE,
} from './client';

const response = (status, body = '') => ({
  ok: status >= 200 && status < 300,
  status,
  text: () => Promise.resolve(typeof body === 'string' ? body : JSON.stringify(body)),
});

beforeEach(() => {
  global.fetch = jest.fn();
});

afterEach(() => {
  delete global.fetch;
});

test('a GET sends the session cookie, no body and no content type', async () => {
  fetch.mockResolvedValue(response(200, { authenticated: true }));

  await expect(apiFetch('/api/check-auth')).resolves.toEqual({ authenticated: true });
  expect(fetch).toHaveBeenCalledWith('/api/check-auth', {
    method: 'GET',
    credentials: 'include',
    signal: undefined,
  });
});

test('a body is sent as JSON', async () => {
  fetch.mockResolvedValue(response(200, { message: 'ok' }));

  await apiFetch('/api/login', { method: 'POST', body: { email: 'a@b.c', password: 'pw' } });

  expect(fetch).toHaveBeenCalledWith('/api/login', {
    method: 'POST',
    credentials: 'include',
    signal: undefined,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: 'a@b.c', password: 'pw' }),
  });
});

test("credentials: 'omit' is passed through for the unauthenticated flows", async () => {
  fetch.mockResolvedValue(response(200, {}));

  await apiFetch('/api/register', { method: 'POST', body: {}, credentials: 'omit' });

  expect(fetch.mock.calls[0][1].credentials).toBe('omit');
});

test('REACT_APP_API_URL prefixes every path', async () => {
  process.env.REACT_APP_API_URL = 'http://localhost:5000';
  try {
    let isolatedFetch;
    jest.isolateModules(() => {
      isolatedFetch = require('./client').apiFetch;
    });
    fetch.mockResolvedValue(response(200, {}));

    await isolatedFetch('/api/user');

    expect(fetch.mock.calls[0][0]).toBe('http://localhost:5000/api/user');
  } finally {
    delete process.env.REACT_APP_API_URL;
  }
});

test('a non-JSON body is returned as text, an empty body as null', async () => {
  fetch.mockResolvedValueOnce(response(200, 'plain text'));
  fetch.mockResolvedValueOnce(response(200, ''));

  await expect(apiFetch('/a')).resolves.toBe('plain text');
  await expect(apiFetch('/b')).resolves.toBeNull();
});

test.each([
  [{ message: 'Invalid credentials' }, 'Invalid credentials'],
  [{ error: 'Customer not found' }, 'Customer not found'],
  ['<html>Internal Server Error</html>', 'Request failed (HTTP 500).'],
])('an error answer %p throws ApiError(%p)', async (body, message) => {
  fetch.mockResolvedValue(response(500, body));

  const error = await apiFetch('/x').catch((e) => e);

  expect(error).toBeInstanceOf(ApiError);
  expect(error).toMatchObject({ message, status: 500 });
});

test('a 429 gets a readable message instead of the raw limit', async () => {
  // What the backend sends for a Flask-Limiter rejection.
  fetch.mockResolvedValue(response(429, { error: 'Too Many Requests', message: '10 per 1 minute' }));

  const error = await apiFetch('/api/login', { method: 'POST', body: {} }).catch((e) => e);

  expect(error).toBeInstanceOf(ApiError);
  expect(error).toMatchObject({ message: RATE_LIMIT_MESSAGE, status: 429 });
  expect(error.data).toEqual({ error: 'Too Many Requests', message: '10 per 1 minute' });
});

test('a 401 runs the unauthorized handler until it is unregistered', async () => {
  const handler = jest.fn();
  const unregister = onUnauthorized(handler);
  fetch.mockResolvedValue(response(401, { message: 'Unauthorized' }));

  await expect(apiFetch('/api/user')).rejects.toMatchObject({ status: 401 });
  expect(handler).toHaveBeenCalledTimes(1);

  unregister();
  await expect(apiFetch('/api/user')).rejects.toMatchObject({ status: 401 });
  expect(handler).toHaveBeenCalledTimes(1);
});

test('other errors do not run the unauthorized handler', async () => {
  const handler = jest.fn();
  const unregister = onUnauthorized(handler);
  fetch.mockResolvedValue(response(403, { message: 'Forbidden' }));

  await expect(apiFetch('/api/get-labeled-data')).rejects.toMatchObject({ status: 403 });
  expect(handler).not.toHaveBeenCalled();
  unregister();
});

test('a network failure throws ApiError with status 0', async () => {
  fetch.mockRejectedValue(new TypeError('Failed to fetch'));

  await expect(apiFetch('/x')).rejects.toMatchObject({ status: 0, message: NETWORK_ERROR_MESSAGE });
});

test('an abort is rethrown unchanged', async () => {
  const abort = new Error('The operation was aborted.');
  abort.name = 'AbortError';
  fetch.mockRejectedValue(abort);

  await expect(apiFetch('/x')).rejects.toBe(abort);
});

test('raw returns the Response once it is OK, and still throws on errors', async () => {
  const ok = response(200, 'a,b\n');
  fetch.mockResolvedValueOnce(ok);
  fetch.mockResolvedValueOnce(response(403, { error: 'Subscription required' }));

  await expect(apiFetch('/csv', { raw: true })).resolves.toBe(ok);
  await expect(apiFetch('/csv', { raw: true })).rejects.toMatchObject({
    status: 403,
    message: 'Subscription required',
  });
});

test('errorMessage prefers the ApiError message and falls back otherwise', () => {
  expect(errorMessage(new ApiError('Nope', 400), 'fallback')).toBe('Nope');
  expect(errorMessage(new Error('internal detail'), 'fallback')).toBe('fallback');
});
