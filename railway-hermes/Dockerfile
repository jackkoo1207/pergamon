# Hermes Agent on Railway — deploy bundle
# Official image: nousresearch/hermes-agent (docs confirm Railway is a supported host
# for long-running Hermes processes: "Render, Railway, Docker, and similar hosts")

FROM nousresearch/hermes-agent:latest

ENV HERMES_HOME=/data
ENV SURFACE=gateway

# Config + secrets are injected at runtime from Railway env vars
COPY entrypoint.sh /usr/local/bin/hermes-entrypoint
RUN chmod +x /usr/local/bin/hermes-entrypoint

EXPOSE 3000

ENTRYPOINT ["/usr/local/bin/hermes-entrypoint"]
