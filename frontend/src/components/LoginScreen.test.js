import React from 'react';
import { fireEvent, screen } from '@testing-library/react';
import LoginScreen from './LoginScreen';
import { ApiError, NETWORK_ERROR_MESSAGE } from '../api/client';
import { apiCalls, apiError, mockApi, renderWithProviders } from '../test-utils';

jest.mock('../api/client', () => ({ ...jest.requireActual('../api/client'), apiFetch: jest.fn() }));

// Google's button lives in a cross-origin iframe; stand in a button that reports a credential.
jest.mock('@react-oauth/google', () => {
  const React = require('react');
  return {
    ...jest.requireActual('@react-oauth/google'),
    GoogleLogin: ({ onSuccess }) =>
      React.createElement(
        'button',
        { type: 'button', onClick: () => onSuccess({ credential: 'tok' }) },
        'Sign in with Google',
      ),
  };
});

const renderLogin = (route = '/login') => renderWithProviders(<LoginScreen />, { route, path: '/login' });

function fillAndSubmit() {
  fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'a@b.c' } });
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'secret1' } });
  fireEvent.click(screen.getByRole('button', { name: 'Log in' }));
}

test('POSTs the credentials to /api/login and goes to /dashboard', async () => {
  mockApi({ '/api/login': { message: 'Login successful' } });
  renderLogin();

  fillAndSubmit();

  expect(await screen.findByRole('heading', { name: 'Dashboard page' })).toBeInTheDocument();
  expect(apiCalls()).toEqual([
    ['/api/login', { method: 'POST', body: { email: 'a@b.c', password: 'secret1' } }],
  ]);
});

test('a 401 shows "Invalid credentials" and stays on the page', async () => {
  mockApi({ '/api/login': apiError(401, 'Invalid credentials') });
  renderLogin();

  fillAndSubmit();

  expect(await screen.findByRole('alert')).toHaveTextContent('Invalid credentials');
  expect(screen.getByRole('button', { name: 'Log in' })).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'Dashboard page' })).not.toBeInTheDocument();
});

test('a network error shows the error text', async () => {
  mockApi({ '/api/login': new ApiError(NETWORK_ERROR_MESSAGE, 0) });
  renderLogin();

  fillAndSubmit();

  expect(await screen.findByRole('alert')).toHaveTextContent(NETWORK_ERROR_MESSAGE);
});

test('Google sign-in POSTs the ID token to /api/authorize/google', async () => {
  mockApi({ '/api/authorize/google': { message: 'Login successful' } });
  renderLogin();

  fireEvent.click(screen.getByRole('button', { name: 'Sign in with Google' }));

  expect(await screen.findByRole('heading', { name: 'Dashboard page' })).toBeInTheDocument();
  expect(apiCalls()).toEqual([
    ['/api/authorize/google', { method: 'POST', body: { credential: 'tok' } }],
  ]);
});

test('a rejected Google sign-in shows the backend message', async () => {
  mockApi({
    '/api/authorize/google': apiError(400, 'An account with this email already exists.'),
  });
  renderLogin();

  fireEvent.click(screen.getByRole('button', { name: 'Sign in with Google' }));

  expect(await screen.findByRole('alert')).toHaveTextContent(
    'An account with this email already exists.',
  );
});

test('links to sign-up and to the forgot-password screen', () => {
  renderLogin();

  expect(screen.getByRole('link', { name: 'Sign up' })).toHaveAttribute('href', '/signup');
  expect(screen.getByRole('link', { name: 'Forgot your password?' })).toHaveAttribute(
    'href',
    '/forgot-password',
  );
});

test('shows the notice passed by the previous screen', () => {
  renderLogin({ pathname: '/login', state: { notice: 'Signup successful! You can now log in.' } });

  expect(screen.getByRole('status')).toHaveTextContent('Signup successful! You can now log in.');
});
