// src/api.js — thin fetch wrapper for the StockScout API.
//
// Base URL is empty by default: in production, Caddy serves this app and
// the FastAPI backend on the SAME origin (different path prefixes), so
// relative paths just work with zero CORS involved. For local dev, where
// Vite (localhost:5173) and uvicorn (localhost:8000) are different ports,
// set VITE_API_BASE=http://localhost:8000 in frontend/.env.local -- see
// backend CORS setup in app/main.py for why that's still safe with cookies.
const API_BASE = import.meta.env.VITE_API_BASE || '';

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

  getScanResults: (jobId, { qualifiedOnly = false, detailed = false } = {}) =>
    request(`/scans/${jobId}/results?qualified_only=${qualifiedOnly}&detailed=${detailed}`),

  resumeScan: (jobId) => request(`/scans/${jobId}/resume`, { method: 'POST' }),

  getHistory: (limit = 20) => request(`/scans?limit=${limit}`),

  // Not a fetch -- returns the raw URL for an EventSource (SSE), which
  // has its own credentialed-cookie behavior (withCredentials) separate
  // from fetch's `credentials` option.
  eventsUrl: (jobId) => `${API_BASE}/scans/${jobId}/events`,
};

export { ApiError };
