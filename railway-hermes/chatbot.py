#!/usr/bin/env python3
"""Chatbot website for the Railway-deployed Hermes agent.

Each message runs a FULL agent session via `hermes chat -Q -q "<msg>"` —
that session has all of Hermes' tools (terminal, file, web, memory, …), the
same agent loop used everywhere. Sessions are RESUMED across turns
(`hermes chat --resume <session_id>`), so the conversation keeps memory and
context like the desktop/CLI chat — not a stateless one-shot per message.

Requires: flask (installed in the image venv).
Optional auth: if CHATBOT_TOKEN is set, the UI asks for it and every POST
must carry it (X-Chatbot-Token header).
"""
import os
import re
import shutil
import subprocess
import threading
import time

from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)
_lock = threading.Lock()

TOKEN = os.environ.get("CHATBOT_TOKEN", "")
HERMES = os.environ.get("HERMES_BIN") or shutil.which("hermes") or "/opt/hermes/.venv/bin/hermes"
TIMEOUT = int(os.environ.get("CHATBOT_TIMEOUT", "180"))

# chat_id -> hermes session_id (per-browser-chat continuity)
SESSIONS = {}
_SESSIONS_ORDER = []
SESSION_CAP = int(os.environ.get("CHATBOT_SESSION_CAP", "30"))
_SESSION_RE = re.compile(r"session_id:\s*(\S+)")

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hermes Chat</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 720px; margin: 0 auto; padding: 16px; background: #0f1115; color: #e6e6e6; }
  h1 { font-size: 1.2rem; }
  #messages { min-height: 60vh; }
  .msg { margin: 10px 0; padding: 10px 14px; border-radius: 10px; white-space: pre-wrap; }
  .user { background: #1d4ed8; margin-left: 40px; }
  .bot { background: #1f2937; margin-right: 40px; }
  .err { background: #7f1d1d; margin-right: 40px; }
  .hint { color: #9ca3af; font-size: .8rem; }
  form { display: flex; gap: 8px; position: sticky; bottom: 0; background: #0f1115; padding: 8px 0; }
  input[type=text] { flex: 1; padding: 10px; border-radius: 8px; border: 1px solid #374151; background: #111827; color: #e6e6e6; }
  button { padding: 10px 16px; border-radius: 8px; border: 0; background: #2563eb; color: white; cursor: pointer; }
  button:disabled { opacity: .5; }
</style>
</head>
<body>
<h1>Hermes <span class="hint">(agentic · tools enabled · multi-turn memory)</span></h1>
<div id="messages"></div>
<form id="form">
  <input type="text" id="msg" placeholder="Ask anything…" autocomplete="off">
  <button id="send" type="submit">Send</button>
</form>
<script>
const chatId = (localStorage.getItem('chatId') || '') || (localStorage.setItem('chatId', Date.now() + '-' + Math.random().toString(36).slice(2, 8)), localStorage.getItem('chatId'));
const TOKEN = {{ token_json }};
async function post(msg) {
  const headers = {'Content-Type': 'application/json'};
  if (TOKEN) headers['X-Chatbot-Token'] = TOKEN;
  const r = await fetch('/api/chat', {method: 'POST', headers, body: JSON.stringify({message: msg, chat_id: chatId})});
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || r.status);
  return d.reply;
}
function add(role, text) {
  const el = document.createElement('div');
  el.className = 'msg ' + role;
  el.textContent = text;
  document.getElementById('messages').appendChild(el);
  window.scrollTo(0, document.body.scrollHeight);
}
document.getElementById('form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = document.getElementById('msg');
  const btn = document.getElementById('send');
  const text = input.value.trim();
  if (!text) return;
  input.value = ''; btn.disabled = true;
  add('user', text);
  add('bot', '…');
  try {
    const reply = await post(text);
    document.querySelector('.msg.bot:last-of-type').textContent = reply;
  } catch (err) {
    const last = document.querySelector('.msg.bot:last-of-type');
    last.className = 'msg err';
    last.textContent = 'Error: ' + err.message;
  }
  btn.disabled = false;
  input.focus();
});
</script>
</body>
</html>"""


def _remember(chat_id: str, session_id: str) -> None:
    if chat_id not in SESSIONS:
        _SESSIONS_ORDER.append(chat_id)
    SESSIONS[chat_id] = session_id
    while len(_SESSIONS_ORDER) > SESSION_CAP:
        old = _SESSIONS_ORDER.pop(0)
        SESSIONS.pop(old, None)


def _run_agent(chat_id: str, message: str) -> str:
    """Run one full agent turn, resuming the chat's session when one exists."""
    cmd = [HERMES, "chat", "-Q"]
    sid = SESSIONS.get(chat_id)
    if sid:
        cmd += ["--resume", sid]
    cmd += ["-q", message]

    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=TIMEOUT,
        env=dict(os.environ, HERMES_HOME=os.environ.get("HERMES_HOME", "/opt/data")),
    )
    out = proc.stdout or ""
    err = proc.stderr or ""

    # -Q prints: [↻ Resumed session …] <final response>\n\nsession_id: <id>
    m = _SESSION_RE.search(out)
    if m:
        _remember(chat_id, m.group(1))
        reply = out[: m.start()].strip()
        # drop the "↻ Resumed session …" banner line from the reply
        reply = "\n".join(l for l in reply.splitlines() if not l.startswith("↻ Resumed session"))
    else:
        reply = out.strip()

    if not reply:
        reply = err.strip() or f"agent exited with code {proc.returncode}"
    return reply


@app.get("/")
def index():
    return render_template_string(PAGE, token_json=("true" if TOKEN else "false"))


@app.post("/api/chat")
def chat():
    if TOKEN and request.headers.get("X-Chatbot-Token", "") != TOKEN:
        return jsonify(error="unauthorized"), 401
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify(error="empty message"), 400
    if len(message) > 4000:
        return jsonify(error="message too long"), 400
    chat_id = (data.get("chat_id") or "default")[:64]

    with _lock:  # one agent process at a time (prototype-grade)
        started = time.time()
        try:
            reply = _run_agent(chat_id, message)
        except subprocess.TimeoutExpired:
            return jsonify(error=f"agent timed out after {TIMEOUT}s"), 504
    return jsonify(reply=reply, elapsed=round(time.time() - started, 1))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "3000")), threaded=True)
