#!/bin/sh
# start-web.sh — runs the DB migration, then starts the API server.
#
# This exists because some PaaS "override the start command" fields
# (Render's Docker Command included) don't reliably parse shell chaining
# syntax like `&&` when you type it directly into the field -- it can end
# up treating the whole string as one literal command instead of two
# chained ones. Putting the logic in an actual script sidesteps that
# entirely: the field just needs to run `sh start-web.sh`, no shell
# parsing of the field's own contents required.
#
# Safe to run on every startup/restart: `alembic upgrade head` is a
# no-op if there's nothing new to migrate.
set -e

python -m alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
