import React, { useContext, useEffect, useRef, useState } from 'react';
import { Navigate, useLocation, useNavigate } from 'react-router-dom';
import { AuthContext } from '../context/AuthContext';
import { apiFetch, errorMessage } from '../api/client';
import { buildSearch, DEFAULT_FILTERS } from '../api/query';
import { safeHttpUrl } from '../utils/url';
import SearchForm from './SearchForm';
import Gallery from './Gallery';
import Spinner from './Spinner';
import Footer from './Footer';
import './Dashboard.css';

// After Stripe Checkout the webhook may land a little after the user does, so the
// dashboard polls the subscription status for up to POLL_MAX_ATTEMPTS seconds.
export const POLL_MAX_ATTEMPTS = 10;
export const POLL_INTERVAL_MS = 1000;

const PAYMENT_PENDING_MESSAGE =
  'Your payment is still being confirmed. Please try again in a moment.';
const SUBSCRIPTION_ENDED_MESSAGE = 'Your subscription is no longer active.';

function sleep(ms, signal) {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener(
      'abort',
      () => {
        clearTimeout(timer);
        resolve();
      },
      { once: true },
    );
  });
}

// Resolves to the status answer as soon as the subscription is active, or null after the last attempt.
async function pollSubscription(signal) {
  for (let attempt = 1; attempt <= POLL_MAX_ATTEMPTS; attempt += 1) {
    if (signal.aborted) {
      return null;
    }
    try {
      const data = await apiFetch('/api/check-subscription-status', { signal });
      if (data && data.subscription_active) {
        return data;
      }
    } catch (err) {
      if (signal.aborted || err.status === 401) {
        throw err;
      }
      // Any other failure just uses up one attempt.
    }
    if (attempt < POLL_MAX_ATTEMPTS) {
      await sleep(POLL_INTERVAL_MS, signal);
    }
  }
  return null;
}

function filenameFromDisposition(header) {
  const match = /filename="?([^";]+)"?/i.exec(header || '');
  return match ? match[1].trim().split(/[\\/]/).pop() : null;
}

const Dashboard = () => {
  const navigate = useNavigate();
  const location = useLocation();
  const { isAuthenticated, setIsAuthenticated } = useContext(AuthContext);

  // 'checking' | 'active' | 'inactive' | 'error'
  const [subscription, setSubscription] = useState('checking');
  const [userEmail, setUserEmail] = useState('');
  // A user without data access who has a Stripe customer (e.g. after a failed payment)
  // is offered the billing portal next to Subscribe.
  const [hasBillingAccount, setHasBillingAccount] = useState(false);
  const [accountError, setAccountError] = useState('');
  const [billingBusy, setBillingBusy] = useState(false);
  const [recheck, setRecheck] = useState(0);
  // Stripe sends the user back to /dashboard?session_id=... after a successful checkout.
  const checkoutSessionRef = useRef(new URLSearchParams(location.search).get('session_id'));

  const [filters, setFilters] = useState(DEFAULT_FILTERS);
  // {sort, query} of the last submitted search: pagination and the CSV use it, not the form.
  const [activeSearch, setActiveSearch] = useState(null);
  const [images, setImages] = useState([]);
  const [currentPage, setCurrentPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [totalItems, setTotalItems] = useState(0);
  const [loading, setLoading] = useState(false);
  const [csvLoading, setCsvLoading] = useState(false);
  const [searchError, setSearchError] = useState('');
  const [csvError, setCsvError] = useState('');
  // Numbers every page request, so only the answer to the newest one is shown.
  const latestRequestRef = useRef(0);

  useEffect(() => {
    if (!isAuthenticated) {
      return undefined;
    }
    const controller = new AbortController();
    const { signal } = controller;

    const checkSubscription = async () => {
      setSubscription('checking');
      setAccountError('');
      const polling = Boolean(checkoutSessionRef.current);
      let status;
      try {
        status = polling
          ? await pollSubscription(signal)
          : await apiFetch('/api/check-subscription-status', { signal });
      } catch (err) {
        if (signal.aborted || err.status === 401) {
          return; // Unmounted, or logged out (the auth context redirects to /login).
        }
        setAccountError(errorMessage(err, 'Could not check your subscription.'));
        setSubscription('error');
        return;
      }
      if (signal.aborted) {
        return;
      }
      const active = Boolean(status && status.subscription_active);
      setHasBillingAccount(Boolean(status && status.has_billing_account));
      if (polling) {
        checkoutSessionRef.current = null;
        navigate('/dashboard', { replace: true }); // drop ?session_id from the URL
      }
      if (polling && !active) {
        setAccountError(PAYMENT_PENDING_MESSAGE);
        setSubscription('error');
      } else {
        setSubscription(active ? 'active' : 'inactive');
      }

      try {
        const user = await apiFetch('/api/user', { signal });
        if (!signal.aborted) {
          setUserEmail((user && user.email) || '');
        }
      } catch {
        // The header just shows no email; a 401 already logged the user out.
      }
    };

    checkSubscription();
    return () => controller.abort();
  }, [isAuthenticated, navigate, recheck]);

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  const handleLogout = async () => {
    try {
      await apiFetch('/api/logout', { method: 'POST' });
    } catch {
      // Log out locally even if the request failed.
    }
    setIsAuthenticated(false);
    navigate('/login');
  };

  // Checkout and the customer portal both answer {url}. Checkout replaces the dashboard
  // (Stripe sends the user back to it); the portal opens in a new tab. That tab is
  // opened during the click, so popup blockers allow it, and pointed at Stripe once the
  // URL arrives; if the browser refused it, the portal opens in this tab instead.
  const redirectToBilling = async (path, fallbackMessage, { newTab = false } = {}) => {
    setAccountError('');
    const tab = newTab ? window.open('', '_blank') : null;
    setBillingBusy(true);
    try {
      const data = await apiFetch(path, { method: 'POST' });
      const url = safeHttpUrl(data && data.url);
      if (!url) {
        throw new Error('The billing response had no URL.');
      }
      if (tab) {
        tab.opener = null;
        tab.location.href = url;
      } else {
        window.location.assign(url);
      }
    } catch (err) {
      if (tab) {
        tab.close();
      }
      setAccountError(errorMessage(err, fallbackMessage));
    } finally {
      setBillingBusy(false);
    }
  };

  const handleSubscribe = () =>
    redirectToBilling('/api/create-checkout-session', 'Could not start checkout.');
  const handleOpenPortal = () =>
    redirectToBilling('/api/create-portal-session', 'Could not open the billing portal.', { newTab: true });

  // The data API answers 403 once the subscription has ended during this session (the
  // Stripe webhook downgraded the user): switch to the Subscribe view, not a search error.
  const showSubscriptionEnded = () => {
    setAccountError(SUBSCRIPTION_ENDED_MESSAGE);
    setSubscription('inactive');
  };

  const fetchPage = async (page, search) => {
    const requestId = latestRequestRef.current + 1;
    latestRequestRef.current = requestId;
    setLoading(true);
    setSearchError('');
    setCsvError('');
    try {
      const data = await apiFetch('/api/get-labeled-data', {
        method: 'POST',
        body: { page, fetch_all: false, ...search },
      });
      if (requestId !== latestRequestRef.current) {
        return; // A newer search or page was requested meanwhile.
      }
      setImages(data.output);
      setCurrentPage(data.current_page);
      setTotalPages(data.total_pages);
      setTotalItems(data.length);
    } catch (err) {
      if (requestId !== latestRequestRef.current) {
        return;
      }
      setImages([]);
      setTotalItems(0);
      if (err.status === 403) {
        showSubscriptionEnded();
      } else {
        setSearchError(errorMessage(err, 'Could not load images.'));
      }
    } finally {
      if (requestId === latestRequestRef.current) {
        setLoading(false);
      }
    }
  };

  const handleSearch = (e) => {
    e.preventDefault();
    let search;
    try {
      search = buildSearch(filters);
    } catch (err) {
      setSearchError(err.message);
      return;
    }
    setActiveSearch(search);
    fetchPage(1, search);
  };

  const handleNextPage = () => {
    if (currentPage < totalPages) {
      fetchPage(currentPage + 1, activeSearch);
    }
  };

  const handlePreviousPage = () => {
    if (currentPage > 1) {
      fetchPage(currentPage - 1, activeSearch);
    }
  };

  // Exports every row matching the last search (the backend streams the whole result).
  const handleDownloadCsv = async () => {
    setCsvLoading(true);
    setCsvError('');
    try {
      const response = await apiFetch('/api/get-labeled-data', {
        method: 'POST',
        body: { fetch_all: true, ...activeSearch },
        raw: true,
      });
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filenameFromDisposition(response.headers.get('Content-Disposition')) || 'data.csv';
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (err) {
      if (err.status === 403) {
        showSubscriptionEnded();
      } else {
        // Kept apart from searchError, so the gallery of the last search stays visible.
        setCsvError(errorMessage(err, 'Could not download the CSV.'));
      }
    } finally {
      setCsvLoading(false);
    }
  };

  const logoutButton = (
    <button type="button" onClick={handleLogout} className="cortex-button cortex-button-ghost">
      Logout
    </button>
  );

  const renderHeader = (action) => (
    <header className="cortex-topbar">
      <div className="cortex-brand">
        <img src={`${process.env.PUBLIC_URL}/logo192.png`} alt="" aria-hidden="true" className="cortex-brand-logo" />
        <h1>Cortex</h1>
        <span className="cortex-brand-tagline">Web image scraper for object detection</span>
      </div>
      <div className="cortex-account">
        {userEmail && (
          <span className="cortex-username" title="Signed in as">
            {userEmail}
          </span>
        )}
        {action}
        {logoutButton}
      </div>
    </header>
  );

  const renderPage = (header, content) => (
    <div className="page-container cortex-dashboard">
      {header}
      <main className="content-wrap">
        <div className="cortex-container">{content}</div>
      </main>
      <Footer />
    </div>
  );

  const accountAlert = accountError ? (
    <p className="cortex-alert" role="alert">
      {accountError}
    </p>
  ) : null;

  if (subscription === 'checking') {
    return renderPage(null, <Spinner label="Checking your subscription" />);
  }

  if (subscription === 'error') {
    return renderPage(
      renderHeader(
        <button type="button" className="cortex-button cortex-button-secondary" onClick={() => setRecheck((n) => n + 1)}>
          Try again
        </button>,
      ),
      accountAlert,
    );
  }

  const portalButton = (
    <button
      type="button"
      className="cortex-button cortex-button-secondary"
      onClick={handleOpenPortal}
      disabled={billingBusy}
    >
      Open Stripe Portal
    </button>
  );

  if (subscription === 'inactive') {
    return renderPage(
      renderHeader(hasBillingAccount && portalButton),
      <>
        {accountAlert}
        <div className="cortex-empty cortex-paywall">
          <h2>Subscribe to search the scraped images</h2>
          <p>An active subscription is required to search the scraped images and export them.</p>
          <button type="button" className="cortex-button" onClick={handleSubscribe} disabled={billingBusy}>
            Subscribe
          </button>
        </div>
      </>,
    );
  }

  return renderPage(
    renderHeader(portalButton),
    <>
      {accountAlert}

      <SearchForm
        filters={filters}
        onChange={(patch) => setFilters((prev) => ({ ...prev, ...patch }))}
        onSubmit={handleSearch}
        onDownload={handleDownloadCsv}
        canDownload={activeSearch !== null && !csvLoading}
      />

      {searchError && (
        <p className="cortex-alert" role="alert">
          {searchError}
        </p>
      )}

      {csvError && (
        <p className="cortex-alert" role="alert">
          {csvError}
        </p>
      )}

      {(loading || csvLoading) && <Spinner label={csvLoading ? 'Preparing CSV' : 'Loading images'} />}

      {!loading && activeSearch === null && (
        <div className="cortex-empty">
          <p>Please click on search to get started!</p>
        </div>
      )}

      {!loading && !csvLoading && activeSearch !== null && !searchError && (
        images.length > 0 ? (
          <>
            <p className="cortex-results-count">
              {totalItems.toLocaleString('en-US')} {totalItems === 1 ? 'image' : 'images'}
            </p>
            <Gallery images={images} />
          </>
        ) : (
          <div className="cortex-empty">
            <p>No images found. Try a different query!</p>
          </div>
        )
      )}

      {!loading && totalItems > 0 && (
        <nav className="pagination-controls" aria-label="Pagination">
          <button
            type="button"
            className="cortex-button cortex-button-secondary"
            onClick={handlePreviousPage}
            disabled={currentPage <= 1}
          >
            Previous
          </button>
          <span className="pagination-info">
            Page {currentPage} of {totalPages}
          </span>
          <button
            type="button"
            className="cortex-button cortex-button-secondary"
            onClick={handleNextPage}
            disabled={currentPage >= totalPages}
          >
            Next
          </button>
        </nav>
      )}
    </>,
  );
};

export default Dashboard;
