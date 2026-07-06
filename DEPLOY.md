# Deploying StockScout

There are two supported deployment shapes:

1. **Same-origin, via Docker Compose + Caddy** (this repo's `docker-compose.yml`) — for a VPS you control. Frontend and API share one domain; Caddy routes by path.
2. **Split services on a PaaS like Render** — no server to manage, but the frontend and API land on two different subdomains, so the session cookie needs different settings (see below).

Both are covered here.

## Option 1: Docker Compose + Caddy (VPS)

```
Browser -> Caddy (edge, TLS) -> FastAPI (web) ┐
                              -> Celery (worker) ┴-> Postgres + Redis
```

```bash
cp .env.docker.example .env
# edit .env: set DOMAIN to your real domain, and a real POSTGRES_PASSWORD

docker compose up -d --build
docker compose logs -f          # watch startup; migrate should complete before web/worker start
```

Visit `https://your-domain` (or `http://localhost` if you left `DOMAIN=localhost`
for a local smoke test — no TLS cert without a real public domain).

Since frontend and API share one origin here, the default cookie settings
(`COOKIE_SAMESITE=lax`) are correct — don't change them for this option.

## Option 2: Render (no server to own)

Render deploys each piece as its own managed service instead of one
docker-compose stack, and handles TLS/domains for you — no Caddy needed.

1. Push this repo to GitHub (see below if you haven't done that yet).
2. In Render, create:
   - **Postgres** (managed)
   - **Key Value** (Render's Redis-compatible managed cache)
   - **Web Service** — Docker runtime, pointed at the repo's root `Dockerfile`
   - **Background Worker** — same repo/Dockerfile, start command overridden to
     `celery -A app.celery_app worker --loglevel=info --concurrency=2`
   - **Static Site** — root directory `frontend`, build command `npm run build`,
     publish directory `dist`
3. Environment variables:
   - On **Web Service** and **Background Worker**: `DATABASE_URL` and
     `REDIS_URL` from the Postgres/Key Value instances Render provisioned,
     plus `COOKIE_SECURE=true`, `COOKIE_SAMESITE=none`, and
     `FRONTEND_ORIGIN=https://<your-static-site>.onrender.com`.
   - The Static Site build needs `VITE_API_BASE=https://<your-web-service>.onrender.com`
     set as a build-time environment variable (Render supports this per-service).

**Why `COOKIE_SAMESITE=none` here specifically:** your Static Site and Web
Service land on two different `*.onrender.com` subdomains. Browsers treat
`onrender.com` subdomains as separate "sites" (it's on the public suffix
list), so a `SameSite=Lax` cookie — correct for the same-origin Caddy
setup above — never gets attached to the frontend's API calls on Render.
`SameSite=None` fixes that, but only works together with `Secure=true`
(i.e. real HTTPS, which Render gives you by default). This was tested
directly: signing up with `COOKIE_SAMESITE=none` and `COOKIE_SECURE=true`
produces a `Set-Cookie` header with both `SameSite=None; Secure` set
correctly.

Render's free tier: web services sleep after 15 minutes idle (30-50s to
wake back up), and the free Postgres expires 30 days after creation. Fine
for trying this out; move to a paid Starter plan (~$7/mo/service) once
you want it always-on.

## Pushing this repo to GitHub

If you haven't done this before:

```bash
cd stockscout                      # wherever you unzipped this project
git init
git add .
git commit -m "Initial commit"
```

Then create an empty repository on GitHub (github.com -> the "+" in the
top right -> "New repository" -> give it a name -> do NOT initialize it
with a README, since you already have code). GitHub will show you a
remote URL after creating it — copy it, then:

```bash
git remote add origin https://github.com/<your-username>/<repo-name>.git
git branch -M main
git push -u origin main
```

It'll prompt for GitHub credentials. GitHub no longer accepts your
account password for this over HTTPS — you need a Personal Access Token
instead (GitHub -> Settings -> Developer settings -> Personal access
tokens -> generate one with `repo` scope, then paste it in as the
password when prompted). Alternatively, set up SSH keys and use the
`git@github.com:...` remote URL instead of `https://` — no token needed
after the one-time key setup.

Once it's pushed, both Render and Railway can connect directly to the
GitHub repo and redeploy automatically on every push to `main`.

## What was and wasn't verified before you run this

Being direct about this rather than implying more than was actually checked:

**Verified for real, in the environment this was built in:**
- `docker compose config` — the full compose file parses and resolves
  correctly (env var interpolation, `depends_on` conditions, volumes,
  healthchecks) against Docker Compose's own schema.
- Both `Dockerfile` and `frontend/Dockerfile` — confirmed syntactically
  valid by actually running `docker build` against them (they progress
  past parsing and into the first `FROM` step; they fail only because
  that sandbox's network policy blocks Docker Hub, not because of
  anything wrong with the Dockerfiles themselves).
- The `Caddyfile` — validated with `caddy validate`, then actually run
  with a live Caddy process pointed at a real FastAPI backend and the
  real built frontend: confirmed `/health`, `/auth/*` proxy correctly,
  arbitrary unknown paths fall back to `index.html` (SPA routing), and a
  full signup -> cookie -> `/auth/me` round trip works through Caddy
  exactly as it would in production (single origin, no CORS).
- The React frontend — built with `npm run build` with no errors, and
  driven through a real signup -> start-scan -> live-progress -> history
  flow in a real (headless) browser against the real backend + a real
  separate Celery worker process.
- `COOKIE_SAMESITE=none` mode (for the Render split-origin path) —
  confirmed the resulting `Set-Cookie` header actually carries
  `SameSite=None; Secure` correctly.

**NOT verified:** the containers actually building and running together
via `docker compose up`, and the Render deployment steps above haven't
been run against a live Render account. That sandbox had no route to any
container registry (Docker Hub, ghcr.io) at all, so base images
(`python:3.12-slim`, `node:20-alpine`, `postgres:16`, `redis:7-alpine`,
`caddy:2-alpine`) couldn't be pulled — confirmed as a network policy
block, not an application error. Every piece was tested individually and
for real where it could be; the remaining step is confirming either
deployment path end-to-end on infrastructure with normal internet access.

## Rolling back a bad deploy (Docker Compose option)

```bash
docker compose down
git checkout <previous-commit>
docker compose up -d --build
```

Postgres data persists in the `stockscout_pg_data` volume across this —
it isn't tied to any particular image build.

