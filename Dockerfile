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

ENV HERMES_HOME=/data
ENV SURFACE=chat

COPY railway-hermes/entrypoint.sh /usr/local/bin/hermes-entrypoint
RUN chmod +x /usr/local/bin/hermes-entrypoint

EXPOSE 3000

ENTRYPOINT ["/usr/local/bin/hermes-entrypoint"]
