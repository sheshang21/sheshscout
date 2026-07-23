"""
app/main.py — FastAPI entrypoint.

Run locally (standalone, without Caddy in front):
    export DATABASE_URL=postgresql+psycopg2://stockscout:stockscout@localhost:5432/stockscout
    export REDIS_URL=redis://localhost:6379/0
    export COOKIE_SECURE=false   # only for http:// local dev — keep true in prod
    uvicorn app.main:app --reload

CORS note: only needed for local dev, where the Vite dev server
(localhost:5173) and this API (localhost:8000) are different origins.
In production, Caddy serves the built frontend and proxies /auth, /scans,
/health to this app on the SAME origin (see Caddyfile) -- no CORS
involved at all there. FRONTEND_ORIGIN below defaults to Vite's port;
override if you run the dev server elsewhere.
"""
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routers import auth as auth_router
from .routers import scans as scans_router
from .routers import intraday_scans as intraday_scans_router

app = FastAPI(title="StockScout API")

FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "http://localhost:5173")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN],
    allow_credentials=True,  # required for the session cookie to be sent/read cross-origin
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router)
app.include_router(scans_router.router)
app.include_router(intraday_scans_router.router)


@app.get("/health")
def health():
    return {"status": "ok"}
