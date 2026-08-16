# Dockerfile — backend image, used by BOTH the `web` and `worker` services
# in docker-compose.yml (they run the same code, just different commands).
#
# NOT used for the Streamlit app (sheshscout.py) -- that app has no
# Dockerfile here since it isn't part of this deployment; it still runs
# however it always has (e.g. `streamlit run sheshscout.py`, Streamlit
# Cloud, etc.), untouched by this migration.

FROM python:3.12-slim

WORKDIR /app

# Fixes a real deploy failure on Render: `celery -A app.celery_app worker ...`
# run as a bare command (Render's Docker Command override, no shell/`python -m`
# involved) does NOT add the working directory to sys.path the way `python -m
# X` or uvicorn's own CLI do -- so `from core.scanner import ...` in
# app/scan_runner.py failed with "ModuleNotFoundError: No module named
# 'core'" even though core/ sits right here at /app/core. uvicorn (used by
# `web`, via start-web.sh) doesn't hit this because its CLI inserts the
# working directory into sys.path itself; celery's CLI does not. Setting
# PYTHONPATH explicitly makes both entrypoints -- and any future override
# someone types into a PaaS "start command" field -- work the same way,
# regardless of whether it's invoked as `celery ...` or `python -m celery ...`.
ENV PYTHONPATH=/app

# curl_cffi needs a C toolchain to build its wheel on some platforms;
# psycopg2-binary does not need build-essential, but keeping this small
# and explicit rather than omitting it and finding out at build time.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-core.txt requirements-server.txt ./
RUN pip install --no-cache-dir -r requirements-server.txt

COPY . .
RUN chmod +x start-web.sh

EXPOSE 8000

# Runs the Alembic migration, then starts uvicorn -- see start-web.sh for
# why this is a script rather than a shell one-liner typed into a "start
# command" field somewhere (those don't reliably parse `&&`).
# Binds to $PORT if set (Render's convention), else 8000 for local/compose use.
CMD ["sh", "start-web.sh"]
