# Hermes Agent on Railway — launch guide

Bundle: `D:\Pergamon\railway-hermes\`
- `Dockerfile`        — FROM nousresearch/hermes-agent:latest
- `entrypoint.sh`     — injects Railway env vars → $HERMES_HOME/.env + starts the surface
- `railway.toml`      — build/deploy config + persistent volume at /data

## Prerequisites (pick one path)

### Path A — Railway web UI (no CLI, ~3 min)
1. Push this folder to a GitHub repo (or zip-upload)
2. Railway → **New Project** → **Deploy from GitHub repo** (or **Deploy from Docker Hub** → `nousresearch/hermes-agent:latest` if you skip the Dockerfile)
3. **Variables** tab — add:
   | Variable | Value |
   |---|---|
   | `SURFACE` | `gateway` (Telegram/Discord bot) or `dashboard` (web UI) or `proxy` (OpenAI-compatible API) |
   | `RAILWAY_MODEL` | e.g. `deepseek/deepseek-chat` or `anthropic/claude-sonnet-4-6` |
   | `OPENROUTER_API_KEY` | your key (or `NOUS_PORTAL_TOKEN` / provider key) |
   | `TELEGRAM_BOT_TOKEN` | if SURFACE=gateway + Telegram (any platform token works) |
   | `HERMES_API_KEY` | if SURFACE=dashboard (auth gate) |
4. **Volumes** tab → add volume mounted at `/data` (persists sessions/memory/skills)
5. Deploy. Logs will show `hermes gateway` connecting.

### Path B — Railway CLI (from this machine)
```bash
npm i -g @railway/cli          # or: iwr https://railway.app/install.ps1 | iex
railway login                  # interactive browser auth — you must do this step
cd D:\Pergamon\railway-hermes
railway init                   # create project
railway up                     # build & deploy
railway variables --set "SURFACE=gateway" --set "OPENROUTER_API_KEY=sk-..." ...
railway volume add hermes-data /data
```

## What runs on Railway (non-interactive surfaces)
| SURFACE | Command | What you get | Needs |
|---|---|---|---|
| `gateway` | `hermes gateway` | Always-on bot on Telegram/Discord/Slack/WhatsApp… | platform bot token |
| `dashboard` | `hermes dashboard` | Web admin + embedded chat (HTTP :3000) | `HERMES_API_KEY` |
| `proxy` | `hermes proxy` | OpenAI-compatible endpoint | provider OAuth/key |
| `chat` | `hermes chat -q "…"` | one-shot (good for cron-style jobs) | — |

## Notes / gotchas
- Desktop app & interactive TUI do NOT run on Railway (no display) — use gateway/dashboard/proxy.
- The docs explicitly bless Railway: *"supported setup for deployments and normal long-running
  Hermes processes on Render, Railway, Docker, and similar hosts."*
- Gateway has no HTTP endpoint → Railway healthcheck will show degraded unless SURFACE=dashboard/proxy.
- Secrets only in env vars → entrypoint writes them to $HERMES_HOME/.env (never commit keys).
- You are NOT logged into the desktop app on the server — gateway/dashboard auth uses tokens, not OAuth.
