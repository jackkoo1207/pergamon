#!/bin/bash
# Hermes entrypoint for Railway
# Injects Railway env vars into Hermes' .env (secrets) and config, probes the
# PostgreSQL database, then starts the requested surface.
set -e

# 1. Determine a WRITABLE HERMES_HOME.
#    Railway volumes can be mounted read-only / permission-restricted even for
#    root (observed: PermissionError '/data/cron'). /opt/data is the image's
#    default home on the container filesystem and is always writable.
if [ -z "${HERMES_HOME:-}" ]; then
  HERMES_HOME=/opt/data
fi
if mkdir -p "$HERMES_HOME/.hermes-wtest" 2>/dev/null; then
  rmdir "$HERMES_HOME/.hermes-wtest" 2>/dev/null || true
  echo "[hermes-entrypoint] HERMES_HOME=$HERMES_HOME (writable)"
else
  HERMES_HOME=/opt/data
  mkdir -p "$HERMES_HOME"
  echo "[hermes-entrypoint] $HERMES_HOME not writable — using fallback HERMES_HOME=/opt/data"
fi
chmod 777 "$HERMES_HOME" 2>/dev/null || true
export HERMES_HOME

# 2. Secrets -> hermes .env (API keys / bot tokens come from Railway env vars)
: > "$HERMES_HOME/.env"
for var in OPENROUTER_API_KEY ANTHROPIC_API_KEY OPENAI_API_KEY DEEPSEEK_API_KEY \
           TELEGRAM_BOT_TOKEN DISCORD_BOT_TOKEN SLACK_BOT_TOKEN WHATSAPP_TOKEN \
           NOUS_PORTAL_TOKEN HERMES_API_KEY; do
  if [ -n "${!var:-}" ]; then
    echo "$var=${!var}" >> "$HERMES_HOME/.env"
  fi
done

# 3. Model/provider (override with RAILWAY_MODEL; defaults to DeepSeek since
#    DEEPSEEK_API_KEY is the key you provisioned on Railway)
MODEL="${RAILWAY_MODEL:-deepseek/deepseek-chat}"
if ! hermes config set model "$MODEL" 2>/dev/null; then
  echo "[hermes-entrypoint] WARNING: 'hermes config set model $MODEL' failed — check HERMES_HOME permissions"
fi

# 4. PostgreSQL probe — "see what is inside" the Railway Postgres.
#    Prints schema + row counts to the logs so the deploy proves DB connectivity.
if [ -n "${DATABASE_URL:-}" ]; then
  echo "===== [hermes-entrypoint] PostgreSQL probe ====="
  if command -v psql >/dev/null 2>&1; then
    psql "$DATABASE_URL" -c '\dt' 2>&1 | head -40 || echo "psql probe failed (see error above)"
    # row counts per table (best-effort)
    TABLES=$(psql "$DATABASE_URL" -Atc "SELECT tablename FROM pg_tables WHERE schemaname='public'" 2>/dev/null | head -20)
    if [ -n "$TABLES" ]; then
      for t in $TABLES; do
        COUNT=$(psql "$DATABASE_URL" -Atc "SELECT count(*) FROM \"$t\"" 2>/dev/null)
        echo "table $t: $COUNT rows"
      done
    else
      echo "No tables in schema 'public' (or database is empty)."
    fi
  else
    echo "psql not installed in image."
  fi
  echo "===== end probe ====="
else
  echo "[hermes-entrypoint] DATABASE_URL not set — skipping DB probe."
fi

# 5. Start the requested surface:
#      gateway   -> always-on messaging bot (Telegram/Discord/Slack/WhatsApp…)
#      dashboard -> web admin UI on :3000
#      proxy     -> OpenAI-compatible endpoint on :3000
#      chatbot   -> simple chatbot website (this repo's Flask app) on :3000
#      chat      -> one-shot `hermes chat -q` (default; auto-inspects the DB)
case "$SURFACE" in
  dashboard) exec hermes dashboard --host 0.0.0.0 --port 3000 ;;
  proxy)     exec hermes proxy --host 0.0.0.0 --port 3000 ;;
  chatbot)   exec /opt/hermes/.venv/bin/python /opt/hermes/chatbot.py ;;
  chat)
    if [ -n "${QUERY:-}" ]; then
      exec hermes chat -q "$QUERY"
    elif [ -n "${DATABASE_URL:-}" ]; then
      exec hermes chat -q "Connect to the PostgreSQL database at the DATABASE_URL environment variable, list all tables with psql or python (psycopg2), and give a short summary of what is inside each one."
    else
      exec hermes chat -q "Say hello and report your model and current status."
    fi
    ;;
  *) exec hermes gateway ;;
esac
