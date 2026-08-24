#!/usr/bin/env python3
"""Minimal chatbot website for the Railway-deployed Hermes agent.

Serves a single-page chat UI; each message runs `hermes chat -q "<msg>"`
(one-shot) in the container and returns the reply.
Requires: flask (installed in the image venv).
Optional auth: if CHATBOT_TOKEN is set, the UI asks for it and every POST
must carry it (X-Chatbot-Token header).
"""
import os
import shutil
import subprocess
import threading

from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)
_lock = threading.Lock()

TOKEN = os.environ.get("CHATBOT_TOKEN", "")
HERMES = shutil.which("hermes") or "/opt/hermes/.venv/bin/hermes"
TIMEOUT = int(os.environ.get("CHATBOT_TIMEOUT", "120"))

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
  form { display: flex; gap: 8px; position: sticky; bottom: 0; background: #0f1115; padding: 8px 0; }
  input[type=text] { flex: 1; padding: 10px; border-radius: 8px; border: 1px solid #374151; background: #111827; color: #e6e6e6; }
  button { padding: 10px 16px; border-radius: 8px; border: 0; background: #2563eb; color: white; cursor: pointer; }
  button:disabled { opacity: .5; }
  .hint { color: #9ca3af; font-size: .8rem; }
</style>
</head>
<body>
<h1>Hermes <span class="hint">(deployed on Railway)</span></h1>
<div id="messages"></div>
<form id="form">
  <input type="text" id="msg" placeholder="Ask anything…" autocomplete="off">
  <button id="send" type="submit">Send</button>
</form>
<script>
const TOKEN = {{ token_json }};
async function post(msg) {
  const headers = {'Content-Type': 'application/json'};
  if (TOKEN) headers['X-Chatbot-Token'] = TOKEN;
  const r = await fetch('/api/chat', {method: 'POST', headers, body: JSON.stringify({message: msg})});
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
    with _lock:  # one hermes process at a time (prototype-grade)
        try:
            proc = subprocess.run(
                [HERMES, "chat", "-q", message],
                capture_output=True, text=True, timeout=TIMEOUT,
                env=dict(os.environ, HERMES_HOME=os.environ.get("HERMES_HOME", "/opt/data")),
            )
        except subprocess.TimeoutExpired:
            return jsonify(error="agent timed out"), 504
    reply = (proc.stdout or "").strip()
    if not reply:
        reply = (proc.stderr or "").strip() or f"agent exited with code {proc.returncode}"
    return jsonify(reply=reply)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "3000")), threaded=True)
