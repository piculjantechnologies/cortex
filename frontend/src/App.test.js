import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import App from './App';
import { ROUTER_FUTURE } from './constants/router';
import { apiCalls, apiError, mockApi } from './test-utils';

jest.mock('./api/client', () => ({ ...jest.requireActual('./api/client'), apiFetch: jest.fn() }));

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname}</output>;
}

function renderApp(route) {
  return render(
    <MemoryRouter initialEntries={[route]} future={ROUTER_FUTURE}>
      <App />
      <LocationProbe />
    </MemoryRouter>,
  );
}

const anonymous = { '/api/check-auth': apiError(401, 'Unauthorized') };
const signedIn = {
  '/api/check-auth': { authenticated: true, user: { email: 'a@b.c', name: 'A' } },
  '/api/check-subscription-status': { subscription_active: true },
  '/api/user': { email: 'a@b.c' },
};

test("'/' without a session shows the login screen", async () => {
  mockApi(anonymous);
  renderApp('/');

  expect(await screen.findByRole('heading', { name: 'Cortex' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Log in' })).toBeInTheDocument();
  expect(screen.getByTestId('location')).toHaveTextContent('/login');
  expect(apiCalls()[0][0]).toBe('/api/check-auth');
});

test.each(['/', '/login'])('a valid session on %s redirects to /dashboard', async (route) => {
  mockApi(signedIn);
  renderApp(route);

  expect(await screen.findByText('a@b.c')).toBeInTheDocument();
  expect(screen.getByTestId('location')).toHaveTextContent('/dashboard');
});

test('check-auth 401 stays on /login', async () => {
  mockApi(anonymous);
  renderApp('/login');

  expect(await screen.findByRole('button', { name: 'Log in' })).toBeInTheDocument();
  expect(screen.getByTestId('location')).toHaveTextContent('/login');
});

test('/dashboard without a session shows the login screen', async () => {
  mockApi(anonymous);
  renderApp('/dashboard');

  expect(await screen.findByRole('button', { name: 'Log in' })).toBeInTheDocument();
  expect(screen.getByTestId('location')).toHaveTextContent('/login');
  expect(apiCalls().map(([path]) => path)).toEqual(['/api/check-auth']);
});

test("an unknown path goes to '/' (and from there to /login)", async () => {
  mockApi(anonymous);
  renderApp('/no-such-page');

  expect(await screen.findByRole('button', { name: 'Log in' })).toBeInTheDocument();
  expect(screen.getByTestId('location')).toHaveTextContent('/login');
});

test('shows a spinner until check-auth answers', async () => {
  let rejectCheck;
  mockApi({
    '/api/check-auth': () =>
      new Promise((resolve, reject) => {
        rejectCheck = reject;
      }),
  });
  renderApp('/login');

  expect(screen.getByRole('status', { name: 'Loading' })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Log in' })).not.toBeInTheDocument();

  rejectCheck(apiError(401, 'Unauthorized'));
  expect(await screen.findByRole('button', { name: 'Log in' })).toBeInTheDocument();
});

test('resetting the password while logged in lands on the login screen with the notice', async () => {
  // The backend ends every session of the account on a reset.
  mockApi({ ...signedIn, '/api/reset-password': { message: 'Password has been reset successfully!' } });
  renderApp('/reset-password/tok');

  fireEvent.change(await screen.findByLabelText('New Password'), { target: { value: 'newpass1' } });
  fireEvent.change(screen.getByLabelText('Confirm New Password'), { target: { value: 'newpass1' } });
  fireEvent.click(screen.getByRole('button', { name: 'Reset Password' }));

  expect(
    await screen.findByText('Your password has been reset. You can now log in.'),
  ).toBeInTheDocument();
  expect(screen.getByTestId('location')).toHaveTextContent('/login');
  expect(apiCalls().map(([path]) => path)).toEqual(['/api/check-auth', '/api/reset-password']);
});

test('the reset-password route is reachable without a session', async () => {
  mockApi(anonymous);
  renderApp('/reset-password/abc');

  expect(await screen.findByRole('heading', { name: 'Reset Password' })).toBeInTheDocument();
});
