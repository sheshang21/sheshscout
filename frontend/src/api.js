// src/api.js — thin fetch wrapper for the StockScout API.
//
// Base URL is empty by default: in production, Caddy serves this app and
// the FastAPI backend on the SAME origin (different path prefixes), so
// relative paths just work with zero CORS involved. For local dev, where
// Vite (localhost:5173) and uvicorn (localhost:8000) are different ports,
// set VITE_API_BASE=http://localhost:8000 in frontend/.env.local -- see
// backend CORS setup in app/main.py for why that's still safe with cookies.
//
// Trailing slash is stripped defensively: VITE_API_BASE=".../"  plus a
// path of "/auth/signup" would otherwise concatenate into "//auth/signup",
// which FastAPI treats as a DIFFERENT path than "/auth/signup" and 404s.
const API_BASE = (import.meta.env.VITE_API_BASE || '').replace(/\/+$/, '');

class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === 'string' ? detail : JSON.stringify(detail));
    this.status = status;
    this.detail = detail;
  }
}

async function request(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    credentials: 'include', // send the session cookie
    headers: {
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...options.headers,
    },
  });

  if (res.status === 204) return null;

  const isJson = res.headers.get('content-type')?.includes('application/json');
  const data = isJson ? await res.json() : await res.text();

  if (!res.ok) {
    throw new ApiError(res.status, isJson ? data.detail ?? data : data);
  }
  return data;
}

export const api = {
  signup: (email, password) =>
    request('/auth/signup', { method: 'POST', body: JSON.stringify({ email, password }) }),

  login: (email, password) =>
    request('/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) }),

  logout: () => request('/auth/logout', { method: 'POST' }),

  me: () => request('/auth/me'),

  startScan: (payload) =>
    request('/scans', { method: 'POST', body: JSON.stringify(payload) }),

  getScan: (jobId) => request(`/scans/${jobId}`),

  getScanResults: (jobId, { qualifiedOnly = false, detailed = false, limit = 10000 } = {}) =>
    request(`/scans/${jobId}/results?qualified_only=${qualifiedOnly}&detailed=${detailed}&limit=${limit}`),

  resumeScan: (jobId) => request(`/scans/${jobId}/resume`, { method: 'POST' }),

  cancelScan: (jobId) => request(`/scans/${jobId}/cancel`, { method: 'POST' }),

  clearHistory: () => request('/scans', { method: 'DELETE' }),

  universeCounts: () => request('/scans/universe/counts'),

  getDebug: (jobId) => request(`/scans/${jobId}/debug`),

  getHistory: (limit = 20) => request(`/scans?limit=${limit}`),

  // Not a fetch -- returns the raw URL for an EventSource (SSE), which
  // has its own credentialed-cookie behavior (withCredentials) separate
  // from fetch's `credentials` option.
  eventsUrl: (jobId) => `${API_BASE}/scans/${jobId}/events`,

  // ── Intraday long/short screeners — same job lifecycle as the positional
  // scans above, mirrored 1:1 against /intraday-scans (see
  // app/routers/intraday_scans.py). Kept as separate methods (not a
  // `scanType` param on the calls above) so a stray typo can't accidentally
  // point a positional scan at the intraday endpoints or vice versa.
  startIntradayScan: (payload) =>
    request('/intraday-scans', { method: 'POST', body: JSON.stringify(payload) }),

  getIntradayScan: (jobId) => request(`/intraday-scans/${jobId}`),

  getIntradayScanResults: (jobId, { qualifiedOnly = false, detailed = true, limit = 10000 } = {}) =>
    request(`/intraday-scans/${jobId}/results?qualified_only=${qualifiedOnly}&detailed=${detailed}&limit=${limit}`),

  resumeIntradayScan: (jobId) => request(`/intraday-scans/${jobId}/resume`, { method: 'POST' }),

  cancelIntradayScan: (jobId) => request(`/intraday-scans/${jobId}/cancel`, { method: 'POST' }),

  clearIntradayHistory: () => request('/intraday-scans', { method: 'DELETE' }),

  getIntradayDebug: (jobId) => request(`/intraday-scans/${jobId}/debug`),

  getIntradayHistory: (limit = 20) => request(`/intraday-scans?limit=${limit}`),

  intradayEventsUrl: (jobId) => `${API_BASE}/intraday-scans/${jobId}/events`,
};

export { ApiError };
