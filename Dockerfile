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
# Flask for the chatbot surface. Install python3-pip via apt (guaranteed to
# work on this Debian-based image) then use the system python3's pip to
# install Flask.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && /usr/bin/python3 -m pip install --no-cache-dir --break-system-packages flask

# HERMES_HOME is decided at runtime by entrypoint.sh (Railway volumes may be
# write-restricted; /opt/data is the image default and always writable).
ENV SURFACE=chat

COPY railway-hermes/entrypoint.sh /usr/local/bin/hermes-entrypoint
COPY railway-hermes/chatbot.py /opt/hermes/chatbot.py
RUN chmod +x /usr/local/bin/hermes-entrypoint

EXPOSE 3000

ENTRYPOINT ["/usr/local/bin/hermes-entrypoint"]
