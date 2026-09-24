import React from 'react';
import { fireEvent, screen } from '@testing-library/react';
import SignupScreen from './SignupScreen';
import { apiCalls, apiError, mockApi, renderWithProviders } from '../test-utils';

jest.mock('../api/client', () => ({ ...jest.requireActual('../api/client'), apiFetch: jest.fn() }));

function fillAndSubmit(password, confirmPassword) {
  fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'a@b.c' } });
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: password } });
  fireEvent.change(screen.getByLabelText('Confirm Password'), { target: { value: confirmPassword } });
  fireEvent.click(screen.getByRole('button', { name: 'Sign Up' }));
}

test('a password mismatch shows an alert and sends no request', async () => {
  mockApi({});
  renderWithProviders(<SignupScreen />, { route: '/signup' });

  fillAndSubmit('secret1', 'secret2');

  expect(await screen.findByRole('alert')).toHaveTextContent('Passwords do not match');
  expect(apiCalls()).toHaveLength(0);
});

test('201 registers without cookies and goes to /login', async () => {
  mockApi({ '/api/register': { message: 'User registered successfully' } });
  renderWithProviders(<SignupScreen />, { route: '/signup' });

  fillAndSubmit('secret1', 'secret1');

  expect(await screen.findByRole('heading', { name: 'Login page' })).toBeInTheDocument();
  expect(apiCalls()).toEqual([
    [
      '/api/register',
      { method: 'POST', body: { email: 'a@b.c', password: 'secret1' }, credentials: 'omit' },
    ],
  ]);
});

test('400 shows the backend message', async () => {
  mockApi({ '/api/register': apiError(400, 'User already exists') });
  renderWithProviders(<SignupScreen />, { route: '/signup' });

  fillAndSubmit('secret1', 'secret1');

  expect(await screen.findByRole('alert')).toHaveTextContent('User already exists');
  expect(screen.getByRole('heading', { name: 'Sign Up' })).toBeInTheDocument();
});

test('password fields mirror the backend length rules', () => {
  renderWithProviders(<SignupScreen />, { route: '/signup' });

  for (const label of ['Password', 'Confirm Password']) {
    expect(screen.getByLabelText(label)).toHaveAttribute('minLength', '6');
    expect(screen.getByLabelText(label)).toHaveAttribute('maxLength', '128');
  }
});
