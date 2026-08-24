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
5. **File attachment**: the chat page has a 📎 button — attach any document (PDF, DOC/DOCX,
   PPT/PPTX, XLS/XLSX, TXT, CSV, MD, …), the agent reads it with its own file tools
   (saved to `$HERMES_HOME/uploads/<user>/`, max 20MB)
6. **Login & per-user isolation**: predefined users only (NO signup) via `CHATBOT_USERS`
   ("user:pass,user2:pass2"; default `test:123456`). Login issues a TTL-limited token
   (`CHATBOT_TOKEN_TTL_HOURS`, default 24h). All state is user-scoped: hermes sessions,
   chat history and uploads are namespaced per user — users can't see each other's data.
7. **Database security (per-user DB access)**: on first login each user gets a dedicated
   Postgres schema `u_<user>` + restricted role (`u_<user>`, random password, stored in
   `hermes_users`). The agent's `DATABASE_URL` is swapped for the user's restricted one
   (with `search_path` forced to their own schema), and the role is revoked from the
   `public` schema — so even if a user asks the agent to read other users' data, the
   **database itself refuses** (permission denied). If the DB is unavailable the agent
   gets NO `DATABASE_URL` at all — the admin URL is never handed to the agent.
8. **Per-user OS isolation (filesystem)**: each user also gets a dedicated Linux account
   (`u_<user>`, home `/home/u_<user>` with mode 0700) and the agent process is launched
   via `runuser` as that account, with its own `HERMES_HOME` and uploads dir. The kernel
   then enforces isolation: user A's agent gets `Permission denied` if it tries to list,
   read, or write user B's home — verified against real Linux (WSL) in this repo's
   verification. (Same-container per-user accounts are the practical equivalent of
   per-user containers on Railway's single-service model.)
9. **Shared EU regulations**: `shared.regulations` (schema `shared`, PUBLIC read grant) is
   seeded automatically at startup from the texts in `railway-hermes/regs/`
   (PPWR 2025/40, LVD 2014/35/EU, Annex VII, Annex VIII) — every user's agent can read
   them. **Per-user documents**: every upload is recorded in the user's own schema
   (`u_<user>.documents`, with text content mirrored for txt/md/csv/json) — the manual
   and Contact.md you upload later land there, visible only to that user's agent.
10. **Compliance cross-check agent**: each user's home has an `AGENTS.md` (loaded by
    hermes from cwd) that instructs the agent to: cross-check the user's manual against
    the shared EU regulations, report a ✅/❌/⚠️ checklist citing each regulation, and
    **ask the user for any missing part instead of guessing**.
11. Starts the surface:
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
