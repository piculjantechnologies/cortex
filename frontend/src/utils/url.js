// Returns `url` only when it is an absolute http(s) URL, so values that come from the
// database or the backend can never become javascript: or data: links and redirects.
export function safeHttpUrl(url) {
  return typeof url === 'string' && /^https?:\/\//i.test(url) ? url : undefined;
}
