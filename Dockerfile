# Root deploy entry for Railway — builds the Hermes agent from railway-hermes/
# Deploy the repo root on Railway; this Dockerfile picks up the bundle.
#
# Railway web UI alternative: set service Root Directory = railway-hermes
# (then the bundle's own Dockerfile + railway.toml are used instead).

FROM nousresearch/hermes-agent:latest

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*
# Flask for the chatbot surface. Try the Hermes venv python first (uv venvs
# ship no pip binary), then system python3 -m pip, then apt python3-pip.
RUN /opt/hermes/.venv/bin/python -m pip install --no-cache-dir flask 2>/dev/null \
    || python3 -m pip install --no-cache-dir flask 2>/dev/null \
    || (apt-get update && apt-get install -y --no-install-recommends python3-pip \
        && python3 -m pip install --no-cache-dir flask)

# HERMES_HOME is decided at runtime by entrypoint.sh (Railway volumes may be
# write-restricted; /opt/data is the image default and always writable).
ENV SURFACE=chat

COPY railway-hermes/entrypoint.sh /usr/local/bin/hermes-entrypoint
COPY railway-hermes/chatbot.py /opt/hermes/chatbot.py
RUN chmod +x /usr/local/bin/hermes-entrypoint

EXPOSE 3000

ENTRYPOINT ["/usr/local/bin/hermes-entrypoint"]
