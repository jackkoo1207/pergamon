# Hermes Agent on Railway — deploy bundle
# Official image: nousresearch/hermes-agent (docs confirm Railway is a supported host
# for long-running Hermes processes: "Render, Railway, Docker, and similar hosts")
#
# Postgres client is installed so the agent can inspect the Railway PostgreSQL
# via its terminal tool (psql "$DATABASE_URL").

FROM nousresearch/hermes-agent:latest

# Install PostgreSQL client (psql) — lets Hermes connect to Railway Postgres
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m pip install --no-cache-dir flask

# HERMES_HOME is decided at runtime by entrypoint.sh (Railway volumes may be
# write-restricted; /opt/data is the image default and always writable).
ENV SURFACE=chat

# Config + secrets are injected at runtime from Railway env vars
COPY entrypoint.sh /usr/local/bin/hermes-entrypoint
COPY chatbot.py /opt/hermes/chatbot.py
RUN chmod +x /usr/local/bin/hermes-entrypoint

EXPOSE 3000

ENTRYPOINT ["/usr/local/bin/hermes-entrypoint"]
