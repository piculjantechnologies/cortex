// Service worker: injects the page script when the toolbar button is clicked and makes
// every Cortex API call, so the page never talks to Cortex itself. Requests carry the
// browser's Cortex session cookie (the user logs in on the Cortex web app).
importScripts('lib/shared.js');

const { DEFAULT_SETTINGS, apiErrorText, originPattern } = self.CortexShared;

async function settings() {
  const stored = await chrome.storage.sync.get(DEFAULT_SETTINGS);
  return { ...DEFAULT_SETTINGS, ...stored };
}

async function callApi(path, body) {
  const { apiUrl } = await settings();
  if (!apiUrl) {
    return { ok: false, status: 0, setup: true, error: 'Set the address of your Cortex in the options first.' };
  }
  const allowed = await chrome.permissions.contains({ origins: [originPattern(apiUrl)] });
  if (!allowed) {
    return { ok: false, status: 0, setup: true, error: `Allow access to ${apiUrl} in the extension's options.` };
  }
  let response;
  try {
    response = await fetch(`${apiUrl}${path}`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      redirect: 'manual', // an API answers; a redirect means the address is not a Cortex API
    });
  } catch {
    return { ok: false, status: 0, setup: true, error: `Cortex (${apiUrl}) could not be reached.` };
  }
  if (response.type === 'opaqueredirect') {
    return {
      ok: false,
      status: 0,
      setup: true,
      error: `${apiUrl} redirects elsewhere instead of answering as a Cortex API. Check the address in the options.`,
    };
  }
  let data = null;
  try {
    data = await response.json();
  } catch {
    // A non-JSON answer (a proxy error page, for example) keeps data null.
  }
  if (!response.ok) {
    return { ok: false, status: response.status, error: apiErrorText(response.status, data && data.message) };
  }
  return { ok: true, status: response.status, data };
}

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (!sender.tab) {
    return false; // only the page script sends messages
  }
  if (message.type === 'lookup') {
    callApi('/api/images/lookup', { urls: message.urls }).then(reply);
    return true; // reply asynchronously
  }
  if (message.type === 'analyse') {
    callApi('/api/images/analyse', { url: message.url }).then(reply);
    return true;
  }
  if (message.type === 'open-app') {
    settings().then(({ appUrl }) => (appUrl ? chrome.tabs.create({ url: appUrl }) : chrome.runtime.openOptionsPage()));
    return false;
  }
  if (message.type === 'open-options') {
    chrome.runtime.openOptionsPage();
    return false;
  }
  return false;
});

// The first install opens the options, where the user enters their Cortex's address.
chrome.runtime.onInstalled.addListener(({ reason }) => {
  if (reason === 'install') {
    chrome.runtime.openOptionsPage();
  }
});

// A click injects the page script into the current tab (activeTab); a second click
// removes the overlay again (the script toggles itself).
chrome.action.onClicked.addListener(async (tab) => {
  try {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ['lib/shared.js', 'content.js'] });
  } catch {
    // Pages the extension may not script (chrome://, the Web Store) are left alone.
    await chrome.action.setBadgeText({ tabId: tab.id, text: '—' });
    await chrome.action.setTitle({ tabId: tab.id, title: 'Cortex cannot run on this page' });
  }
});
