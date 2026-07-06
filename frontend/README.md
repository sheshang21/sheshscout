# StockScout frontend

React SPA for the scan dashboard: auth, scan controls, live progress via
SSE, sortable/filterable results, and history.

## Local dev (against a locally-running backend)

```bash
npm install
echo "VITE_API_BASE=http://localhost:8000" > .env.local
npm run dev
```

This assumes the backend is already running on port 8000 (see the repo
root README / DEPLOY.md for how to start Postgres, Redis, the API, and a
Celery worker). Vite runs on :5173 by default; the backend's CORS config
(`FRONTEND_ORIGIN` in `app/main.py`) already defaults to that.

## Production build

```bash
npm run build
```

Outputs to `dist/`. No `VITE_API_BASE` is set for the production build on
purpose — the built app uses relative API paths, which only work because
Caddy serves this app and the FastAPI backend on the same origin (see the
root `Caddyfile`). This is exactly what `frontend/Dockerfile` does; you
shouldn't normally need to run this build manually.

## Structure

- `src/api.js` — the only file that knows API paths/shapes. Everything
  else calls through it.
- `src/components/` — one component per concern (auth, scan form, live
  progress, results table, history). `src/App.jsx` wires them together
  with plain `useState`, no router — the app has exactly two views.
