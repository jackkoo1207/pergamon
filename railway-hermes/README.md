# Hermes Agent on Railway — launch guide (v2: PostgreSQL edition)

Bundle: `D:\Pergamon\railway-hermes\` + root `Dockerfile` / `railway.toml` (repo-root deploy)
- `Dockerfile`            — FROM nousresearch/hermes-agent:latest + **postgresql-client (psql)**
- `entrypoint.sh`         — injects env → $HERMES_HOME/.env, **probes PostgreSQL**, starts the surface
- `railway.toml`          — build config + persistent volume at /data

## What it does
1. Writes API keys from Railway env vars into Hermes' `.env`
2. Sets the model (`RAILWAY_MODEL`, default `deepseek/deepseek-chat` — works with `DEEPSEEK_API_KEY`)
3. **PostgreSQL probe**: connects to `DATABASE_URL` with `psql`, prints all tables + row counts
   to the logs — so you can "see what is inside" in Railway's Logs tab, no agent needed
4. **Session memory → Postgres**: chat history + chat→hermes-session mapping persisted to
   `hermes_chats` / `hermes_messages` (conversations survive restarts)
5. **PDF attachment**: the chat page has a 📎 button — upload a PDF, the agent reads it
   with its own file tools (saved to `$HERMES_HOME/uploads/`, max 20MB)
6. Starts the surface:
   | SURFACE | What you get |
   |---|---|
   | `chat` (default) | one-shot `hermes chat -q` — auto-prompt: *"connect to DATABASE_URL, list tables, summarize what's inside"* |
   | `gateway` | always-on bot (Telegram/Discord/Slack/WhatsApp…) — needs a platform bot token |
   | `dashboard` | web admin + embedded chat on :3000 — needs `HERMES_API_KEY` |
   | `proxy` | OpenAI-compatible endpoint on :3000 |

## Required Railway variables (already set by you ✅)
| Variable | Value |
|---|---|
| `DATABASE_URL` | auto-provided by the Railway Postgres plugin |
| `DEEPSEEK_API_KEY` | your DeepSeek key |
| `SURFACE` | optional — leave unset for the DB-inspection one-shot |

## How the agent "sees inside" Postgres
Hermes runs with full terminal access in the container, and `DATABASE_URL` is in its
environment — so it can run:
```bash
psql "$DATABASE_URL" -c '\dt'
psql "$DATABASE_URL" -c 'SELECT * FROM "mytable" LIMIT 10;'
python -c "import psycopg2,os; ..."   # python is in the image
```
The startup probe already prints `\dt` + row counts to the logs.

## Deploy
1. Push this repo (D:\Pergamon) to GitHub — already at github.com/jackkoo1207/pergamon
2. Railway → your Pergamon service → Settings → **Root Directory = `railway-hermes`** (or leave
   root — the root `Dockerfile`/`railway.toml` now build the same thing)
3. Add `SURFACE=chat` (optional), keep `DATABASE_URL` + `DEEPSEEK_API_KEY`
4. Deploy → open **Logs** → see the PostgreSQL probe output → then the agent's summary

## Notes / gotchas
- No HTTP endpoint on chat/gateway surfaces → no healthcheck configured (Railway marks
  the service healthy while the process runs; one-shot chat exits 0 after answering).
- `railway.toml` mounts `/data` — sessions, memory and skills persist across redeploys.
- If you want the agent to keep running 24/7 instead of one-shot, set `SURFACE=gateway`
  (requires e.g. `TELEGRAM_BOT_TOKEN`).
- Desktop app / interactive TUI cannot run on Railway (no display).
