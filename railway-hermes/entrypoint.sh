#!/bin/bash
# Hermes entrypoint for Railway
# Injects Railway env vars into Hermes' .env (secrets) and config, then starts the chosen surface.
set -e

mkdir -p "$HERMES_HOME"

# 1. Secrets -> hermes .env (API keys / bot tokens come from Railway env vars)
: > "$HERMES_HOME/.env"
for var in OPENROUTER_API_KEY ANTHROPIC_API_KEY OPENAI_API_KEY DEEPSEEK_API_KEY \
           TELEGRAM_BOT_TOKEN DISCORD_BOT_TOKEN SLACK_BOT_TOKEN WHATSAPP_TOKEN \
           NOUS_PORTAL_TOKEN HERMES_API_KEY; do
  if [ -n "${!var:-}" ]; then
    echo "$var=${!var}" >> "$HERMES_HOME/.env"
  fi
done

# 2. Model/provider (override with RAILWAY_MODEL env, e.g. deepseek/deepseek-chat)
if [ -n "${RAILWAY_MODEL:-}" ]; then
  hermes config set model "$RAILWAY_MODEL"
fi

# 3. Start the requested surface: gateway (bots) | dashboard (web admin) | proxy (OpenAI-compatible)
case "$SURFACE" in
  dashboard) exec hermes dashboard --host 0.0.0.0 --port 3000 ;;
  proxy)     exec hermes proxy --host 0.0.0.0 --port 3000 ;;
  chat)      exec hermes chat -q "${QUERY:-Say hello and report your model and status.}" ;;
  *)         exec hermes gateway ;;   # default: messaging bot mode
esac
