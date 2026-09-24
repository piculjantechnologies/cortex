import React from 'react';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { useLocation } from 'react-router-dom';
import Dashboard, { POLL_MAX_ATTEMPTS } from './Dashboard';
import { apiCalls, apiError, mockApi, renderWithProviders } from '../test-utils';

jest.mock('../api/client', () => ({ ...jest.requireActual('../api/client'), apiFetch: jest.fn() }));

const DEFAULT_FILTER = { $and: [{ width: { $gte: 100 } }, { label_quality: { $gte: 0.5 } }] };

// The top-level conditions of a sent filter.
const conditionsOf = (filter) => (filter.$and ? filter.$and : [filter]);
const conditionOn = (filter, key) => conditionsOf(filter).find((condition) => key in condition);

const IMAGE = {
  _id: 'PT::1',
  url: 'https://images.example/dog.jpg',
  width: 400,
  height: 200,
  object_detection: { dog: [[0.1, 0.2, 0.5, 0.6]] },
  label_quality_score: 0.9,
};

function pageResponse(options, images = [IMAGE]) {
  const page = options.body.page;
  return { output: images, length: 30, current_page: page, total_pages: 2, has_next_page: page < 2 };
}

const ACTIVE = {
  '/api/check-subscription-status': { subscription_active: true },
  '/api/user': { email: 'a@b.c' },
  '/api/get-labeled-data': (options) => pageResponse(options),
};

const INACTIVE = {
  '/api/check-subscription-status': { subscription_active: false },
  '/api/user': { email: 'a@b.c' },
};

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname + location.search}</output>;
}

function renderDashboard(route = '/dashboard', auth = { isAuthenticated: true }) {
  return renderWithProviders(
    <>
      <Dashboard />
      <LocationProbe />
    </>,
    { route, path: '/dashboard', auth },
  );
}

async function renderActiveDashboard(handlers = {}) {
  mockApi({ ...ACTIVE, ...handlers });
  renderDashboard();
  await screen.findByText('a@b.c');
}

const labeledDataBodies = () =>
  apiCalls()
    .filter(([path]) => path === '/api/get-labeled-data')
    .map(([, options]) => options.body);

async function search() {
  fireEvent.click(screen.getByRole('button', { name: 'Search' }));
  await screen.findByText('Page 1 of 2');
}

const originalLocation = window.location;

beforeEach(() => {
  delete window.location;
  window.location = { ...originalLocation, assign: jest.fn() };
  // jsdom has no window.open; by default the browser "blocks" the portal tab.
  window.open = jest.fn(() => null);
});

afterEach(() => {
  window.location = originalLocation;
  jest.restoreAllMocks();
  jest.useRealTimers();
});

describe('access and subscription', () => {
  test('without a session it goes to /login and sends no request', async () => {
    mockApi(ACTIVE);
    renderDashboard('/dashboard', { isAuthenticated: false });

    expect(await screen.findByRole('heading', { name: 'Login page' })).toBeInTheDocument();
    expect(apiCalls()).toHaveLength(0);
  });

  test('an active subscription shows the user and the search form', async () => {
    await renderActiveDashboard();

    expect(screen.getByRole('button', { name: 'Open Stripe Portal' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Search' })).toBeInTheDocument();
    expect(screen.getByText('Please click on search to get started!')).toBeInTheDocument();
  });

  test('an inactive subscription shows Subscribe and Logout instead of redirecting', async () => {
    mockApi(INACTIVE);
    renderDashboard();

    expect(await screen.findByText('a@b.c')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Subscribe' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Logout' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Open Stripe Portal' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Search' })).not.toBeInTheDocument();
    expect(apiCalls().map(([path]) => path)).not.toContain('/api/create-checkout-session');
    expect(window.location.assign).not.toHaveBeenCalled();
  });

  test('Subscribe POSTs create-checkout-session and redirects to the returned url', async () => {
    mockApi({
      ...INACTIVE,
      '/api/create-checkout-session': { url: 'https://checkout.stripe.com/c/pay/cs_1' },
    });
    renderDashboard();

    fireEvent.click(await screen.findByRole('button', { name: 'Subscribe' }));

    await waitFor(() =>
      expect(window.location.assign).toHaveBeenCalledWith('https://checkout.stripe.com/c/pay/cs_1'),
    );
    expect(apiCalls()).toContainEqual(['/api/create-checkout-session', { method: 'POST' }]);
  });

  test('a failed checkout shows the error instead of "Loading..."', async () => {
    mockApi({
      ...INACTIVE,
      '/api/create-checkout-session': apiError(502, 'Payment provider unavailable'),
    });
    renderDashboard();

    fireEvent.click(await screen.findByRole('button', { name: 'Subscribe' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Payment provider unavailable');
    expect(screen.queryByText('Loading...')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Subscribe' })).toBeEnabled();
    expect(window.location.assign).not.toHaveBeenCalled();
  });

  test('a checkout answer without an http(s) url is not followed', async () => {
    // eslint-disable-next-line no-script-url -- the hostile value under test
    mockApi({ ...INACTIVE, '/api/create-checkout-session': { url: 'javascript:alert(1)' } });
    renderDashboard();

    fireEvent.click(await screen.findByRole('button', { name: 'Subscribe' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not start checkout.');
    expect(window.location.assign).not.toHaveBeenCalled();
  });

  test('a past_due user is refused a new checkout and can open the billing portal', async () => {
    mockApi({
      ...INACTIVE,
      '/api/check-subscription-status': {
        subscription_active: false,
        subscription_status: 'past_due',
        has_billing_account: true,
      },
      '/api/create-checkout-session': apiError(
        409,
        'Your last payment failed. Update your payment method in the billing portal.',
      ),
      '/api/create-portal-session': { url: 'https://billing.stripe.com/p/session/1' },
    });
    renderDashboard();

    fireEvent.click(await screen.findByRole('button', { name: 'Subscribe' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Your last payment failed.');
    expect(window.location.assign).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Open Stripe Portal' }));
    await waitFor(() =>
      expect(window.location.assign).toHaveBeenCalledWith('https://billing.stripe.com/p/session/1'),
    );
    expect(apiCalls()).toContainEqual(['/api/create-portal-session', { method: 'POST' }]);
  });

  test('Logout POSTs /api/logout and goes to /login', async () => {
    mockApi({ ...INACTIVE, '/api/logout': { message: 'Logged out successfully' } });
    renderDashboard();

    fireEvent.click(await screen.findByRole('button', { name: 'Logout' }));

    expect(await screen.findByRole('heading', { name: 'Login page' })).toBeInTheDocument();
    expect(apiCalls()).toContainEqual(['/api/logout', { method: 'POST' }]);
  });

  test('Open Stripe Portal POSTs with no body and, with the new tab blocked, follows the url here', async () => {
    await renderActiveDashboard({
      '/api/create-portal-session': { url: 'https://billing.stripe.com/p/session/1' },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Open Stripe Portal' }));

    await waitFor(() =>
      expect(window.location.assign).toHaveBeenCalledWith('https://billing.stripe.com/p/session/1'),
    );
    expect(apiCalls()).toContainEqual(['/api/create-portal-session', { method: 'POST' }]);
  });

  test('Open Stripe Portal opens the portal in a new tab when the browser allows it', async () => {
    const tab = { opener: window, location: { href: '' }, close: jest.fn() };
    window.open.mockReturnValue(tab);
    await renderActiveDashboard({
      '/api/create-portal-session': { url: 'https://billing.stripe.com/p/session/1' },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Open Stripe Portal' }));

    expect(window.open).toHaveBeenCalledWith('', '_blank');
    await waitFor(() => expect(tab.location.href).toBe('https://billing.stripe.com/p/session/1'));
    expect(tab.opener).toBeNull();
    expect(window.location.assign).not.toHaveBeenCalled();
  });

  test('a failed portal request closes the new tab and shows the error', async () => {
    const tab = { opener: window, location: { href: '' }, close: jest.fn() };
    window.open.mockReturnValue(tab);
    await renderActiveDashboard({ '/api/create-portal-session': apiError(500, 'Stripe is down.') });

    fireEvent.click(screen.getByRole('button', { name: 'Open Stripe Portal' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Stripe is down.');
    expect(tab.close).toHaveBeenCalled();
    expect(tab.location.href).toBe('');
  });

  test('Subscribe keeps checkout in the same tab', async () => {
    mockApi({ ...INACTIVE, '/api/create-checkout-session': { url: 'https://checkout.stripe.com/c/pay/cs_1' } });
    renderDashboard();

    fireEvent.click(await screen.findByRole('button', { name: 'Subscribe' }));

    await waitFor(() =>
      expect(window.location.assign).toHaveBeenCalledWith('https://checkout.stripe.com/c/pay/cs_1'),
    );
    expect(window.open).not.toHaveBeenCalled();
  });

  test('a failing status check shows an error and can be retried', async () => {
    mockApi({
      ...ACTIVE,
      '/api/check-subscription-status': (options, call) =>
        call === 1 ? apiError(500, 'Internal server error') : { subscription_active: true },
    });
    renderDashboard();

    expect(await screen.findByRole('alert')).toHaveTextContent('Internal server error');
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));

    expect(await screen.findByRole('button', { name: 'Search' })).toBeInTheDocument();
  });
});

describe('returning from Stripe Checkout', () => {
  test('polls until the webhook activated the subscription, then drops session_id', async () => {
    jest.useFakeTimers();
    mockApi({
      ...ACTIVE,
      '/api/check-subscription-status': (options, call) => ({ subscription_active: call >= 3 }),
    });
    renderDashboard('/dashboard?session_id=cs_1');

    expect(await screen.findByText('a@b.c', {}, { timeout: 10000 })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Search' })).toBeInTheDocument();
    expect(apiCalls().filter(([path]) => path === '/api/check-subscription-status')).toHaveLength(3);
    expect(screen.getByTestId('location')).toHaveTextContent(/^\/dashboard$/);
  });

  test(`${POLL_MAX_ATTEMPTS} failed polls show the timeout message`, async () => {
    jest.useFakeTimers();
    mockApi({
      ...ACTIVE,
      '/api/check-subscription-status': (options, call) =>
        call % 2 ? { subscription_active: false } : apiError(500, 'Internal server error'),
    });
    renderDashboard('/dashboard?session_id=cs_1');

    expect(await screen.findByRole('alert', {}, { timeout: 20000 })).toHaveTextContent(
      /payment is still being confirmed/i,
    );
    expect(apiCalls().filter(([path]) => path === '/api/check-subscription-status')).toHaveLength(
      POLL_MAX_ATTEMPTS,
    );
    expect(screen.queryByRole('button', { name: 'Subscribe' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent(/^\/dashboard$/);
  });
});

describe('search, pagination and CSV', () => {
  test('the search body is exactly {page, fetch_all, sort, filter} with the form defaults', async () => {
    await renderActiveDashboard();

    await search();

    expect(labeledDataBodies()).toEqual([{ page: 1, fetch_all: false, sort: 'quality', filter: DEFAULT_FILTER }]);
  });

  test('a Min width of 0 sends no width condition', async () => {
    await renderActiveDashboard();

    fireEvent.change(screen.getByLabelText('Min width'), { target: { value: '0' } });
    await search();

    expect(conditionOn(labeledDataBodies()[0].filter, 'width')).toBeUndefined();
  });

  test('an emptied number field falls back to the backend default', async () => {
    await renderActiveDashboard();

    fireEvent.change(screen.getByLabelText('Min label quality'), { target: { value: '' } });
    await search();

    expect(conditionOn(labeledDataBodies()[0].filter, 'label_quality')).toEqual({ label_quality: { $gte: 0.5 } });
  });

  test('a class chip cycles any -> included -> excluded -> any', async () => {
    await renderActiveDashboard();

    fireEvent.click(screen.getByRole('button', { name: 'dog' }));
    fireEvent.click(screen.getByRole('button', { name: 'cat' }));
    fireEvent.click(screen.getByRole('button', { name: 'cat, included' }));
    fireEvent.click(screen.getByRole('button', { name: 'car' }));
    fireEvent.click(screen.getByRole('button', { name: 'car, included' }));
    fireEvent.click(screen.getByRole('button', { name: 'car, excluded' }));
    await search();

    expect(screen.getByRole('button', { name: 'dog, included' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'cat, excluded' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'car' })).toBeInTheDocument();
    expect(conditionsOf(labeledDataBodies()[0].filter).slice(0, 2)).toEqual([
      { class: 'dog' },
      { $not: { class: 'cat' } },
    ]);
  });

  test('Match any, Clear and the sort order are sent', async () => {
    await renderActiveDashboard();

    const clear = screen.getByRole('button', { name: 'Clear' });
    expect(clear).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'dog' }));
    fireEvent.click(screen.getByRole('button', { name: 'cat' }));
    fireEvent.click(screen.getByRole('radio', { name: 'Match any' }));
    fireEvent.change(screen.getByLabelText('Sort by'), { target: { value: 'newest' } });
    await search();

    expect(labeledDataBodies()[0].sort).toBe('newest');
    expect(conditionsOf(labeledDataBodies()[0].filter)[0]).toEqual({ $or: [{ class: 'dog' }, { class: 'cat' }] });

    fireEvent.click(clear);
    expect(clear).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));
    await waitFor(() => expect(labeledDataBodies()).toHaveLength(2));
    expect(labeledDataBodies()[1].filter).toEqual(DEFAULT_FILTER);
  });

  test('count, box size and "only these classes" apply to the included classes', async () => {
    await renderActiveDashboard();

    expect(screen.getByLabelText('Min objects')).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'person' }));
    fireEvent.change(screen.getByLabelText('Min objects'), { target: { value: '2' } });
    fireEvent.change(screen.getByLabelText('Largest box at least'), { target: { value: '10' } });
    fireEvent.click(screen.getByRole('checkbox', { name: 'Only these classes (no other objects)' }));
    await search();

    const [person, others] = conditionsOf(labeledDataBodies()[0].filter);
    expect(person).toEqual({ class: 'person', count: { $gte: 2 }, max_box_area: { $gte: 0.1 } });
    expect(others.$not.$or).toHaveLength(19);
    expect(others.$not.$or).not.toContainEqual({ class: 'person' });
  });

  test('the Advanced tab starts from the simple filters and sends its JSON', async () => {
    await renderActiveDashboard();

    fireEvent.click(screen.getByRole('button', { name: 'dog' }));
    fireEvent.click(screen.getByRole('tab', { name: 'Advanced' }));
    const editor = screen.getByLabelText('Filter (JSON)');
    expect(JSON.parse(editor.value)).toEqual({
      $and: [{ class: 'dog' }, { width: { $gte: 100 } }, { label_quality: { $gte: 0.5 } }],
    });

    fireEvent.change(editor, { target: { value: '{"class": "cat", "count": {"$gte": 3}}' } });
    expect(screen.getByText('Valid JSON')).toBeInTheDocument();
    await search();

    expect(labeledDataBodies()[0].filter).toEqual({ class: 'cat', count: { $gte: 3 } });
  });

  test('invalid advanced JSON is reported without a request', async () => {
    await renderActiveDashboard();

    fireEvent.click(screen.getByRole('tab', { name: 'Advanced' }));
    fireEvent.change(screen.getByLabelText('Filter (JSON)'), { target: { value: '{"class": ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('The filter is not valid JSON');
    expect(labeledDataBodies()).toEqual([]);
  });

  test('"The Filters tab" reloads the simple filters into the editor', async () => {
    await renderActiveDashboard();

    fireEvent.click(screen.getByRole('tab', { name: 'Advanced' }));
    fireEvent.change(screen.getByLabelText('Filter (JSON)'), { target: { value: '{}' } });
    fireEvent.click(screen.getByRole('tab', { name: 'Filters' }));
    fireEvent.click(screen.getByRole('button', { name: 'cat' }));
    fireEvent.click(screen.getByRole('tab', { name: 'Advanced' }));
    expect(screen.getByLabelText('Filter (JSON)').value).toBe('{}'); // edits are kept
    fireEvent.click(screen.getByRole('button', { name: 'The Filters tab' }));

    expect(JSON.parse(screen.getByLabelText('Filter (JSON)').value).$and[0]).toEqual({ class: 'cat' });
  });

  test('an example replaces the advanced filter', async () => {
    await renderActiveDashboard();

    fireEvent.click(screen.getByRole('tab', { name: 'Advanced' }));
    fireEvent.click(screen.getByRole('button', { name: 'Two or more people and nothing else' }));

    const filter = JSON.parse(screen.getByLabelText('Filter (JSON)').value);
    expect(filter.$and[0]).toEqual({ class: 'person', count: { $gte: 2 } });
  });

  test('the gallery shows images with alt text and their label quality', async () => {
    await renderActiveDashboard();

    await search();

    const image = screen.getByRole('img', { name: 'Image with dog' });
    expect(image).toHaveAttribute('src', IMAGE.url);
    expect(image).toHaveAttribute('referrerPolicy', 'no-referrer');
    expect(screen.getByRole('link', { name: 'Image with dog' })).toHaveAttribute('href', IMAGE.url);
    expect(screen.getByText('90.0%')).toBeInTheDocument();
  });

  test('bounding boxes render once the image has loaded', async () => {
    jest.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(280);
    jest.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(280);
    await renderActiveDashboard();
    await search();
    const tile = within(screen.getByRole('link', { name: 'Image with dog' }));
    expect(tile.queryByText('dog')).not.toBeInTheDocument();

    fireEvent.load(screen.getByRole('img', { name: 'Image with dog' }));

    // 400x200 image contained in 280x280: scale 0.7, vertical offset 70.
    const label = await tile.findByText('dog');
    // The box is the label's positioned parent <div>, which has no accessible role.
    // eslint-disable-next-line testing-library/no-node-access
    expect(label.parentElement).toHaveStyle({
      left: '28px',
      top: '98px',
      width: '112px',
      height: '56px',
    });
  });

  test('a stored javascript: url renders no link and no image source', async () => {
    await renderActiveDashboard({
      '/api/get-labeled-data': (options) =>
        // eslint-disable-next-line no-script-url -- the hostile value under test
        pageResponse(options, [{ ...IMAGE, _id: 'PT::2', url: 'javascript:alert(1)' }]),
    });

    await search();

    expect(screen.getByText('Image unavailable')).toBeInTheDocument();
    const hrefs = screen.queryAllByRole('link').map((link) => link.getAttribute('href'));
    expect(hrefs.filter((href) => /^javascript:/i.test(href))).toEqual([]);
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
  });

  test('Next sends page 2 with the submitted filters, not the edited ones', async () => {
    await renderActiveDashboard();
    await search();

    fireEvent.change(screen.getByLabelText('Min width'), { target: { value: '500' } });
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await screen.findByText('Page 2 of 2');

    expect(labeledDataBodies()[1]).toEqual({ page: 2, fetch_all: false, sort: 'quality', filter: DEFAULT_FILTER });
    fireEvent.click(screen.getByRole('button', { name: 'Previous' }));
    await screen.findByText('Page 1 of 2');
    expect(labeledDataBodies()[2]).toEqual({ page: 1, fetch_all: false, sort: 'quality', filter: DEFAULT_FILTER });
  });

  test('a search error shows its message and no gallery', async () => {
    await renderActiveDashboard({
      '/api/get-labeled-data': apiError(500, 'An unexpected error occurred.'),
    });

    fireEvent.click(screen.getByRole('button', { name: 'Search' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('An unexpected error occurred.');
    expect(screen.queryByText('No images found. Try a different query!')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Search' })).toBeInTheDocument();
  });

  test('a 403 from the search (subscription ended) switches to the Subscribe view', async () => {
    await renderActiveDashboard({
      '/api/get-labeled-data': apiError(403, 'An active subscription is required.'),
    });

    fireEvent.click(screen.getByRole('button', { name: 'Search' }));

    expect(await screen.findByRole('button', { name: 'Subscribe' })).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Your subscription is no longer active.');
    expect(screen.getByRole('button', { name: 'Logout' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Open Stripe Portal' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Search' })).not.toBeInTheDocument();
  });

  test('an older search that answers last does not replace the newer results', async () => {
    const pending = {};
    await renderActiveDashboard({
      '/api/get-labeled-data': (options) =>
        new Promise((resolve) => {
          pending[conditionOn(options.body.filter, 'width').width.$gte] = () =>
            resolve(
              conditionOn(options.body.filter, 'width').width.$gte === 500
                ? { output: [IMAGE], length: 1, current_page: 1, total_pages: 1 }
                : {
                    output: [{ ...IMAGE, _id: 'PT::old', object_detection: { cat: [[0, 0, 1, 1]] } }],
                    length: 30,
                    current_page: 1,
                    total_pages: 2,
                  },
            );
        }),
    });

    fireEvent.click(screen.getByRole('button', { name: 'Search' }));
    fireEvent.change(screen.getByLabelText('Min width'), { target: { value: '500' } });
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));

    pending[500]();
    expect(await screen.findByText('Page 1 of 1')).toBeInTheDocument();
    await act(async () => {
      pending[100]();
    });

    expect(screen.getByText('Page 1 of 1')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'Image with dog' })).toBeInTheDocument();
    expect(screen.queryByRole('img', { name: 'Image with cat' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
  });

  test('Download CSV is disabled until a search has run', async () => {
    await renderActiveDashboard();

    expect(screen.getByRole('button', { name: 'Download CSV' })).toBeDisabled();
  });

  test('Download CSV sends one fetch_all request, clicks an <a download> and revokes the URL', async () => {
    const csvResponse = {
      blob: () => Promise.resolve(new Blob(['_id,url\n'], { type: 'text/csv' })),
      headers: {
        get: (name) =>
          name.toLowerCase() === 'content-disposition' ? 'attachment; filename=export.csv' : null,
      },
    };
    await renderActiveDashboard({
      '/api/get-labeled-data': (options) => (options.raw ? csvResponse : pageResponse(options)),
    });
    URL.createObjectURL = jest.fn(() => 'blob:csv-1');
    URL.revokeObjectURL = jest.fn();
    const clicks = [];
    jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function recordClick() {
      clicks.push({ href: this.getAttribute('href'), download: this.getAttribute('download') });
    });
    await search();

    fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }));

    await waitFor(() => expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:csv-1'));
    expect(labeledDataBodies()).toEqual([
      { page: 1, fetch_all: false, sort: 'quality', filter: DEFAULT_FILTER },
      { fetch_all: true, sort: 'quality', filter: DEFAULT_FILTER },
    ]);
    expect(apiCalls().find(([, options]) => options.raw)).toEqual([
      '/api/get-labeled-data',
      { method: 'POST', body: { fetch_all: true, sort: 'quality', filter: DEFAULT_FILTER }, raw: true },
    ]);
    expect(clicks).toEqual([{ href: 'blob:csv-1', download: 'export.csv' }]);
    expect(await screen.findByText('Page 1 of 2')).toBeInTheDocument();
  });

  test('a failed CSV download shows its error and keeps the gallery', async () => {
    await renderActiveDashboard({
      '/api/get-labeled-data': (options) =>
        options.raw ? apiError(502, 'Request failed (HTTP 502).') : pageResponse(options),
    });
    await search();

    fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Request failed (HTTP 502).');
    expect(screen.getByRole('img', { name: 'Image with dog' })).toBeInTheDocument();
    expect(screen.getByText('Page 1 of 2')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Download CSV' })).toBeEnabled();
  });

  test('a 403 from the CSV download switches to the Subscribe view', async () => {
    await renderActiveDashboard({
      '/api/get-labeled-data': (options) =>
        options.raw ? apiError(403, 'An active subscription is required.') : pageResponse(options),
    });
    await search();

    fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }));

    expect(await screen.findByRole('button', { name: 'Subscribe' })).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Your subscription is no longer active.');
    expect(screen.queryByRole('button', { name: 'Download CSV' })).not.toBeInTheDocument();
  });
});
