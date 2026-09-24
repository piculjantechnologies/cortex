import React from 'react';
import { fireEvent, screen } from '@testing-library/react';
import ForgotPasswordScreen from './ForgotPasswordScreen';
import { apiFetch, RATE_LIMIT_MESSAGE } from '../api/client';
import { apiCalls, apiError, mockApi, renderWithProviders } from '../test-utils';

jest.mock('../api/client', () => ({ ...jest.requireActual('../api/client'), apiFetch: jest.fn() }));

const originalFetch = global.fetch;

afterEach(() => {
  global.fetch = originalFetch;
});

function submitEmail() {
  fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'a@b.c' } });
  fireEvent.click(screen.getByRole('button', { name: 'Send Reset Link' }));
}

test('shows the neutral status message', async () => {
  mockApi({ '/api/forgot-password': { message: 'ok' } });
  renderWithProviders(<ForgotPasswordScreen />, { route: '/forgot-password' });

  submitEmail();

  expect(await screen.findByRole('status')).toHaveTextContent(
    /if that email belongs to an account with a password, a reset link has been sent/i,
  );
  expect(apiCalls()).toEqual([
    ['/api/forgot-password', { method: 'POST', body: { email: 'a@b.c' }, credentials: 'omit' }],
  ]);
});

test('an error answer shows an alert', async () => {
  mockApi({ '/api/forgot-password': apiError(500, 'An unexpected error occurred.') });
  renderWithProviders(<ForgotPasswordScreen />, { route: '/forgot-password' });

  submitEmail();

  expect(await screen.findByRole('alert')).toHaveTextContent('An unexpected error occurred.');
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
});

test('a rate-limited answer shows a readable message, not the raw limit', async () => {
  // The real apiFetch, fed the body the backend sends for a Flask-Limiter rejection.
  apiFetch.mockImplementation(jest.requireActual('../api/client').apiFetch);
  global.fetch = jest.fn().mockResolvedValue({
    ok: false,
    status: 429,
    text: () => Promise.resolve(JSON.stringify({ error: 'Too Many Requests', message: '3 per 1 hour' })),
  });
  renderWithProviders(<ForgotPasswordScreen />, { route: '/forgot-password' });

  submitEmail();

  expect(await screen.findByRole('alert')).toHaveTextContent(RATE_LIMIT_MESSAGE);
  expect(screen.queryByText(/per 1 hour/)).not.toBeInTheDocument();
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
});

test('links back to the login screen', () => {
  renderWithProviders(<ForgotPasswordScreen />, { route: '/forgot-password' });

  expect(screen.getByRole('link', { name: 'Return to Log In' })).toHaveAttribute('href', '/login');
});
