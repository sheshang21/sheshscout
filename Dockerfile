# Dockerfile — backend image, used by BOTH the `web` and `worker` services
# in docker-compose.yml (they run the same code, just different commands).
#
# NOT used for the Streamlit app (sheshscout.py) -- that app has no
# Dockerfile here since it isn't part of this deployment; it still runs
# however it always has (e.g. `streamlit run sheshscout.py`, Streamlit
# Cloud, etc.), untouched by this migration.

FROM python:3.12-slim

WORKDIR /app

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
