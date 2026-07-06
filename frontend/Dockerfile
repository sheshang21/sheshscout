# frontend/Dockerfile — builds the React app, then hands the static
# output to Caddy in the same image. This is the `edge` service in
# docker-compose.yml: one container is both the reverse proxy and the
# frontend's static file server, which is why there's no separate nginx
# here (Caddy already does both jobs, plus automatic TLS -- see Caddyfile).
#
# NOTE: build context is the REPO ROOT, not this directory (see the
# `context: .` / `dockerfile: frontend/Dockerfile` split in
# docker-compose.yml) -- that's the only way this stage can also COPY
# the root-level Caddyfile. All paths below are relative to repo root.

FROM node:20-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ .
# No VITE_API_BASE set here on purpose: the built app uses relative API
# paths (see src/api.js), which only works because Caddy serves both the
# frontend and the API on the same origin in production. If you ever
# split them onto different domains, this needs revisiting.
RUN npm run build

FROM caddy:2-alpine
COPY --from=build /app/dist /srv/frontend
COPY Caddyfile /etc/caddy/Caddyfile
