// Options: the Cortex API and web app URLs, and the host permission for the API.
const { DEFAULT_SETTINGS, normaliseBase, originPattern } = window.CortexShared;

const form = document.getElementById('settings');
const apiInput = document.getElementById('api-url');
const appInput = document.getElementById('app-url');
const status = document.getElementById('status');

function show(text, isError = false) {
  status.textContent = text;
  status.className = isError ? 'error' : 'ok';
}

async function load() {
  const settings = await chrome.storage.sync.get(DEFAULT_SETTINGS);
  apiInput.value = settings.apiUrl;
  appInput.value = settings.appUrl;
}

async function save(apiUrl, appUrl) {
  const api = normaliseBase(apiUrl);
  const app = appUrl.trim() ? normaliseBase(appUrl) : api;
  if (!api || !app) {
    show('Enter http(s) addresses, e.g. https://cortex.example.com.', true);
    return;
  }
  // Asked from the click, as Chrome requires; already granted origins are not asked again.
  const granted = await chrome.permissions.request({ origins: [originPattern(api)] });
  if (!granted) {
    show(`Without access to ${api} the extension cannot reach Cortex.`, true);
    return;
  }
  await chrome.storage.sync.set({ apiUrl: api, appUrl: app });
  apiInput.value = api;
  appInput.value = app;
  show('Saved. Log in on the Cortex web app, then click the Cortex button on any page.');
}

form.addEventListener('submit', (event) => {
  event.preventDefault();
  save(apiInput.value, appInput.value);
});

load();
