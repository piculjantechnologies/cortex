import React, { useContext } from 'react';
import { act, render, screen } from '@testing-library/react';
import { AuthContext, AuthProvider } from './AuthContext';
import { apiFetch } from '../api/client';

const response = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  text: () => Promise.resolve(JSON.stringify(body)),
});

function AuthState() {
  const { isAuthenticated, checking } = useContext(AuthContext);
  let state = 'logged out';
  if (checking) {
    state = 'checking';
  } else if (isAuthenticated) {
    state = 'logged in';
  }
  return <p>{state}</p>;
}

const renderProvider = () =>
  render(
    <AuthProvider>
      <AuthState />
    </AuthProvider>,
  );

beforeEach(() => {
  global.fetch = jest.fn();
});

afterEach(() => {
  delete global.fetch;
});

test('a valid session cookie logs the user in', async () => {
  fetch.mockResolvedValue(response(200, { authenticated: true, user: { email: 'a@b.c' } }));
  renderProvider();

  expect(screen.getByText('checking')).toBeInTheDocument();
  expect(await screen.findByText('logged in')).toBeInTheDocument();
  expect(fetch).toHaveBeenCalledWith('/api/check-auth', expect.objectContaining({ credentials: 'include' }));
});

test('check-auth 401 leaves the user logged out', async () => {
  fetch.mockResolvedValue(response(401, { authenticated: false }));
  renderProvider();

  expect(await screen.findByText('logged out')).toBeInTheDocument();
});

test('an unreachable backend counts as logged out', async () => {
  fetch.mockRejectedValue(new TypeError('Failed to fetch'));
  renderProvider();

  expect(await screen.findByText('logged out')).toBeInTheDocument();
});

test('a 401 from any later request logs the user out', async () => {
  fetch.mockResolvedValueOnce(response(200, { authenticated: true }));
  renderProvider();
  await screen.findByText('logged in');

  fetch.mockResolvedValueOnce(response(401, { message: 'Unauthorized' }));
  await act(async () => {
    await apiFetch('/api/user').catch(() => {});
  });

  expect(screen.getByText('logged out')).toBeInTheDocument();
});
