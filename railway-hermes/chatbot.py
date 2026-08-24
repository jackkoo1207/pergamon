#!/usr/bin/env python3
"""Chatbot website for the Railway-deployed Hermes agent.

Each message runs a FULL agent session via `hermes chat -Q -q "<msg>"` —
that session has all of Hermes' tools (terminal, file, web, memory, …), the
same agent loop used everywhere. Sessions are RESUMED across turns
(`hermes chat --resume <session_id>`), so the conversation keeps memory and
context like the desktop/CLI chat — not a stateless one-shot per message.

Session memory is persisted to PostgreSQL (DATABASE_URL, e.g. Railway Postgres):
  hermes_chats    chat_id -> hermes session_id   (survives app restarts)
  hermes_messages every user/agent turn          (full conversation log)
The app degrades gracefully to in-memory-only when DATABASE_URL is unset or
unreachable.

Requires: flask, psycopg2-binary (installed in the image).
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

try:
    import psycopg2
    _PSYCOPG2 = True
except Exception:
    psycopg2 = None
    _PSYCOPG2 = False

app = Flask(__name__)
_lock = threading.Lock()

TOKEN = os.environ.get("CHATBOT_TOKEN", "")
HERMES = os.environ.get("HERMES_BIN") or shutil.which("hermes") or "/opt/hermes/.venv/bin/hermes"
TIMEOUT = int(os.environ.get("CHATBOT_TIMEOUT", "180"))
DB_URL = os.environ.get("DATABASE_URL", "")
MAX_UPLOAD_MB = int(os.environ.get("CHATBOT_MAX_UPLOAD_MB", "20"))
UPLOAD_DIR = os.path.join(os.environ.get("HERMES_HOME", "/opt/data"), "uploads")

# chat_id -> hermes session_id (per-browser-chat continuity)
SESSIONS = {}
_SESSIONS_ORDER = []
SESSION_CAP = int(os.environ.get("CHATBOT_SESSION_CAP", "30"))
_SESSION_RE = re.compile(r"session_id:\s*(\S+)")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hermes_chats (
    chat_id            TEXT PRIMARY KEY,
    hermes_session_id  TEXT NOT NULL,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS hermes_messages (
    id         BIGSERIAL PRIMARY KEY,
    chat_id    TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_hermes_messages_chat ON hermes_messages (chat_id, created_at);
"""

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
<h1>Hermes <span class="hint">(agentic · tools · multi-turn memory · saved to Postgres)</span></h1>
<div id="messages"></div>
<form id="form">
  <input type="text" id="msg" placeholder="Ask anything… (or attach a file)" autocomplete="off">
  <label id="filelabel" for="file" title="Attach a file (pdf, docx, pptx, xlsx, txt, …)">📎</label>
  <input type="file" id="file" accept=".pdf,.doc,.docx,.ppt,.pptx,.xls,.xlsx,.txt,.csv,.md,.json,.rtf,.odt,.ods" hidden>
  <button id="send" type="submit">Send</button>
</form>
<script>
const chatId = (localStorage.getItem('chatId') || '') || (localStorage.setItem('chatId', Date.now() + '-' + Math.random().toString(36).slice(2, 8)), localStorage.getItem('chatId'));
const TOKEN = {{ token_json }};
let attached = null;
async function post(msg) {
  const fd = new FormData();
  fd.append('message', msg);
  fd.append('chat_id', chatId);
  if (attached) fd.append('file', attached);
  const headers = {};
  if (TOKEN) headers['X-Chatbot-Token'] = TOKEN;
  const r = await fetch('/api/chat', {method: 'POST', headers, body: fd});
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
document.getElementById('file').addEventListener('change', (e) => {
  const f = e.target.files[0];
  if (!f) { attached = null; document.getElementById('filelabel').textContent = '📎'; return; }
  if (f.size > 20 * 1024 * 1024) { alert('File too large (max 20MB)'); e.target.value = ''; return; }
  attached = f;
  document.getElementById('filelabel').textContent = '📎 ' + f.name;
});
document.getElementById('form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = document.getElementById('msg');
  const btn = document.getElementById('send');
  const text = input.value.trim();
  if (!text && !attached) return;
  input.value = ''; btn.disabled = true;
  add('user', (attached ? '📎 ' + attached.name + '\n' : '') + text || '(attached file)');
  add('bot', '…');
  try {
    const reply = await post(text);
    document.querySelector('.msg.bot:last-of-type').textContent = reply;
  } catch (err) {
    const last = document.querySelector('.msg.bot:last-of-type');
    last.className = 'msg err';
    last.textContent = 'Error: ' + err.message;
  }
  attached = null;
  document.getElementById('filelabel').textContent = '📎';
  document.getElementById('file').value = '';
  btn.disabled = false;
  input.focus();
});
</script>
</body>
</html>"""


# ---------------------------------------------------------------- DB layer
_db = None


def _db_connect() -> object:
    """Return a live psycopg2 connection, or None when DB is unavailable."""
    global _db
    if not DB_URL or not _PSYCOPG2:
        return None
    try:
        if _db is None or _db.closed:
            _db = psycopg2.connect(DB_URL)
        return _db
    except Exception:
        return None


def _db_exec(sql: str, params: tuple = ()) -> bool:
    conn = _db_connect()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def _db_query(sql: str, params: tuple = ()) -> list:
    conn = _db_connect()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    except Exception:
        return []


def _db_init() -> None:
    if _db_exec(_SCHEMA):
        print(f"[chatbot] PostgreSQL session storage ready ({DB_URL.split('@')[-1]})")
    else:
        print("[chatbot] WARNING: PostgreSQL unavailable — session memory is in-memory only")


def _db_load_sessions() -> None:
    for chat_id, sid in _db_query("SELECT chat_id, hermes_session_id FROM hermes_chats"):
        _remember(chat_id, sid, persist=False)


def _db_save_session(chat_id: str, sid: str) -> None:
    _db_exec(
        "INSERT INTO hermes_chats (chat_id, hermes_session_id) VALUES (%s, %s) "
        "ON CONFLICT (chat_id) DO UPDATE SET hermes_session_id = EXCLUDED.hermes_session_id, "
        "updated_at = now()",
        (chat_id, sid),
    )


def _db_log_message(chat_id: str, role: str, content: str) -> None:
    _db_exec(
        "INSERT INTO hermes_messages (chat_id, role, content) VALUES (%s, %s, %s)",
        (chat_id, role, content),
    )


# ------------------------------------------------------------- session map
def _save_upload(file_obj):
    """Save an uploaded file to UPLOAD_DIR; return the path, or None on reject."""
    if file_obj is None or not file_obj.filename:
        return None
    file_obj.stream.seek(0, os.SEEK_END)
    size = file_obj.stream.tell()
    file_obj.stream.seek(0)
    if size <= 0 or size > MAX_UPLOAD_MB * 1024 * 1024:
        return None
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    dest = os.path.join(UPLOAD_DIR, f"{int(time.time() * 1000)}-{os.path.basename(file_obj.filename)}")
    file_obj.save(dest)
    print(f"[chatbot] saved upload: {dest} ({size} bytes)")
    return dest


def _remember(chat_id: str, session_id: str, persist: bool = True) -> None:
    if chat_id not in SESSIONS:
        _SESSIONS_ORDER.append(chat_id)
    SESSIONS[chat_id] = session_id
    while len(_SESSIONS_ORDER) > SESSION_CAP:
        old = _SESSIONS_ORDER.pop(0)
        SESSIONS.pop(old, None)
    if persist:
        _db_save_session(chat_id, session_id)


def _run_agent(chat_id: str, message: str) -> str:
    """Run one full agent turn, resuming the chat's session when one exists."""
    sid = SESSIONS.get(chat_id)
    cmd = [HERMES, "chat", "-Q"]
    if sid:
        cmd += ["--resume", sid]
    cmd += ["-q", message]

    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=TIMEOUT,
        env=dict(os.environ, HERMES_HOME=os.environ.get("HERMES_HOME", "/opt/data")),
    )

    # Resume can fail (e.g. session lost after a container restart) — retry fresh
    if sid and proc.returncode != 0:
        SESSIONS.pop(chat_id, None)
        _db_exec("DELETE FROM hermes_chats WHERE chat_id = %s", (chat_id,))
        proc = subprocess.run(
            [HERMES, "chat", "-Q", "-q", message], capture_output=True, text=True,
            timeout=TIMEOUT,
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


# ------------------------------------------------------------------ routes
@app.get("/")
def index():
    return render_template_string(PAGE, token_json=("true" if TOKEN else "false"))


@app.post("/api/chat")
def chat():
    if TOKEN and request.headers.get("X-Chatbot-Token", "") != TOKEN:
        return jsonify(error="unauthorized"), 401

    # multipart (file upload) or JSON
    if request.files:
        message = (request.form.get("message") or "").strip()
        chat_id = (request.form.get("chat_id") or "default")[:64]
        f = request.files.get("file")
        path = _save_upload(f)
        if path is None and f is not None:
            return jsonify(error="invalid or too-large file (max %dMB)" % MAX_UPLOAD_MB), 400
        if path:
            message = f"[User attached a file at: {path}]\n\n{message or 'Please read the attached file and summarize it.'}"
    else:
        data = request.get_json(silent=True) or {}
        message = (data.get("message") or "").strip()
        chat_id = (data.get("chat_id") or "default")[:64]

    if not message:
        return jsonify(error="empty message"), 400
    if len(message) > 8000:
        return jsonify(error="message too long"), 400

    _db_log_message(chat_id, "user", message)
    with _lock:  # one agent process at a time (prototype-grade)
        started = time.time()
        try:
            reply = _run_agent(chat_id, message)
        except subprocess.TimeoutExpired:
            return jsonify(error=f"agent timed out after {TIMEOUT}s"), 504
    _db_log_message(chat_id, "agent", reply)
    return jsonify(reply=reply, elapsed=round(time.time() - started, 1))


if __name__ == "__main__":
    _db_init()
    _db_load_sessions()
    print(f"[chatbot] surface ready (hermes={HERMES}, token_auth={bool(TOKEN)})")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "3000")), threaded=True)
