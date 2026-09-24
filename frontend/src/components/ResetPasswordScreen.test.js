import React from 'react';
import { fireEvent, screen } from '@testing-library/react';
import ResetPasswordScreen from './ResetPasswordScreen';
import { apiCalls, apiError, mockApi, renderWithProviders } from '../test-utils';

jest.mock('../api/client', () => ({ ...jest.requireActual('../api/client'), apiFetch: jest.fn() }));

const renderReset = (route) =>
  renderWithProviders(<ResetPasswordScreen />, { route, path: '/reset-password/:token' });

function submitPasswords(password, confirmPassword = password) {
  fireEvent.change(screen.getByLabelText('New Password'), { target: { value: password } });
  fireEvent.change(screen.getByLabelText('Confirm New Password'), { target: { value: confirmPassword } });
  fireEvent.click(screen.getByRole('button', { name: 'Reset Password' }));
}

test('sends a crafted token in the body of the fixed route, never as a path', async () => {
  mockApi({ '/api/reset-password': { message: 'Password has been reset successfully!' } });
  renderReset('/reset-password/..%2Flogout');

  submitPasswords('newpass1');

  expect(await screen.findByRole('heading', { name: 'Login page' })).toBeInTheDocument();
  expect(apiCalls()).toEqual([
    [
      '/api/reset-password',
      { method: 'POST', body: { token: '../logout', password: 'newpass1' }, credentials: 'omit' },
    ],
  ]);
  expect(apiCalls().some(([path]) => path === '/api/logout')).toBe(false);
});

test('a password mismatch shows an alert and sends no request', async () => {
  mockApi({});
  renderReset('/reset-password/abc');

  submitPasswords('newpass1', 'newpass2');

  expect(await screen.findByRole('alert')).toHaveTextContent('Passwords do not match');
  expect(apiCalls()).toHaveLength(0);
});

test('an invalid or expired token shows the backend message', async () => {
  mockApi({ '/api/reset-password': apiError(400, 'Invalid or expired token.') });
  renderReset('/reset-password/abc');

  submitPasswords('newpass1');

  expect(await screen.findByRole('alert')).toHaveTextContent('Invalid or expired token.');
});

test('offers a way back to the login screen', () => {
  renderReset('/reset-password/abc');

  expect(screen.getByRole('link', { name: 'Back to log in' })).toHaveAttribute('href', '/login');
});
