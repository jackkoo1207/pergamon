#!/usr/bin/env python3
from __future__ import annotations

"""Chatbot website for the Railway-deployed Hermes agent.

Each message runs a FULL agent session via `hermes chat -Q -q "<msg>"` —
that session has all of Hermes' tools (terminal, file, web, memory, …), the
same agent loop used everywhere. Sessions are RESUMED across turns
(`hermes chat --resume <session_id>`), so the conversation keeps memory and
context like the desktop/CLI chat — not a stateless one-shot per message.

Users & security:
  - Login required: predefined users only (NO account creation). Users are
    configured via CHATBOT_USERS="user1:pass1,user2:pass2"; defaults to
    "test:123456".
  - Successful login returns a random session token (in-memory, TTL-limited).
    Every /api/chat call must carry it (X-Chatbot-Token header).
  - All state is user-scoped: hermes sessions, chat history and uploads are
    namespaced per user (chat_id = "<user>:<chat_id>", uploads stored under
    uploads/<user>/), so users cannot see or resume each other's sessions.

Session memory is persisted to PostgreSQL (DATABASE_URL, e.g. Railway Postgres):
  hermes_chats    chat_id -> hermes session_id   (survives app restarts)
  hermes_messages every user/agent turn          (full conversation log)
The app degrades gracefully to in-memory-only when DATABASE_URL is unset or
unreachable.

Requires: flask, psycopg2-binary (installed in the image).
"""
import os
import json
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from flask import Flask, Response, jsonify, redirect, request

try:
    import psycopg2
    from psycopg2 import sql as pgsql
    _PSYCOPG2 = True
except Exception:
    psycopg2 = None
    pgsql = None
    _PSYCOPG2 = False

app = Flask(__name__)
_lock = threading.Lock()

HERMES = os.environ.get("HERMES_BIN") or shutil.which("hermes") or "/opt/hermes/.venv/bin/hermes"
TIMEOUT = int(os.environ.get("CHATBOT_TIMEOUT", "180"))
DB_URL = os.environ.get("DATABASE_URL", "")
MAX_UPLOAD_MB = int(os.environ.get("CHATBOT_MAX_UPLOAD_MB", "20"))
UPLOAD_ROOT = os.path.join(os.environ.get("HERMES_HOME", "/opt/data"), "uploads")
TOKEN_TTL_HOURS = int(os.environ.get("CHATBOT_TOKEN_TTL_HOURS", "24"))

# Predefined users: "user1:pass1,user2:pass2". NO signup — fixed list only.
_USERS_RAW = os.environ.get("CHATBOT_USERS", "test:123456,test2:654321")
USERS = {}
for _pair in _USERS_RAW.split(","):
    if ":" in _pair:
        _u, _p = _pair.split(":", 1)
        USERS[_u.strip()] = _p

# token -> {"user": str, "created": float}
TOKENS = {}
_TOKENS_LOCK = threading.Lock()

# chat_id -> hermes session_id (per-user, per-browser-chat continuity)
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
CREATE TABLE IF NOT EXISTS hermes_users (
    username      TEXT PRIMARY KEY,
    schema_name   TEXT NOT NULL,
    role_name     TEXT NOT NULL,
    role_password TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS oauth_states (
    state      TEXT PRIMARY KEY,
    username   TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# Shared (all-user) EU-regulation store. PUBLIC grants = every user's agent can
# read it, nobody can write except the app (admin).
_SHARED_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS shared;
CREATE TABLE IF NOT EXISTS shared.regulations (
    id         SERIAL PRIMARY KEY,
    title      TEXT NOT NULL UNIQUE,
    celex      TEXT,
    content    TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
GRANT USAGE ON SCHEMA shared TO PUBLIC;
GRANT SELECT ON shared.regulations TO PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA shared GRANT SELECT ON TABLES TO PUBLIC;
"""

# username -> restricted DATABASE_URL (per-user role); built at login
USER_DB = {}

# username -> isolated Linux account (u_<safe>, home 0700) for agent processes
USER_OS = {}
_PROVIDER_ENV_KEYS = ["DEEPSEEK_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
                      "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "XAI_API_KEY",
                      "MISTRAL_API_KEY", "NOUS_PORTAL_TOKEN"]

PAGE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "page.html"), encoding="utf-8").read()


# ---------------------------------------------------------------- auth
def _issue_token(username: str) -> str:
    token = secrets.token_urlsafe(32)
    with _TOKENS_LOCK:
        TOKENS[token] = {"user": username, "created": time.time()}
    return token


def _auth_user() -> str | None:
    """Return the username for a valid X-Chatbot-Token, or None."""
    token = request.headers.get("X-Chatbot-Token", "")
    if not token:
        return None
    with _TOKENS_LOCK:
        entry = TOKENS.get(token)
        if not entry:
            return None
        if time.time() - entry["created"] > TOKEN_TTL_HOURS * 3600:
            TOKENS.pop(token, None)
            return None
        return entry["user"]


# ---------------------------------------------------------------- DB layer
_db = None


def _db_connect():
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
    if _db_exec(_SCHEMA) and _db_exec(_SHARED_SCHEMA):
        print(f"[chatbot] PostgreSQL session storage ready ({DB_URL.split('@')[-1]})")
        _seed_regulations()
    else:
        print("[chatbot] WARNING: PostgreSQL unavailable — session memory is in-memory only")


def _seed_regulations() -> None:
    """Upsert EU-regulation texts from REG_SEED_DIR into shared.regulations.
    Idempotent; runs at every startup so the shared store self-heals."""
    d = os.environ.get("REG_SEED_DIR", "")
    if not d or not os.path.isdir(d):
        return
    for fn in sorted(os.listdir(d)):
        if not fn.lower().endswith(".txt"):
            continue
        p = os.path.join(d, fn)
        try:
            with open(p, encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except OSError:
            continue
        title = os.path.splitext(fn)[0]
        if _db_exec(
            "INSERT INTO shared.regulations (title, content) VALUES (%s, %s) "
            "ON CONFLICT (title) DO UPDATE SET content = EXCLUDED.content, updated_at = now()",
            (title, content),
        ):
            print(f"[chatbot] seeded shared regulation: {title} ({len(content)} chars)")


def _restricted_url(username: str, role: str, password: str, schema: str) -> str:
    """Swap the admin user/password for the per-user role in DATABASE_URL and
    force its search_path to the user's own schema."""
    from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
    u = urlparse(DB_URL)
    qs = dict(parse_qsl(u.query))
    qs["options"] = f"-c search_path={schema}"
    return urlunparse((
        u.scheme, f"{role}:{password}@{u.hostname}:{u.port or 5432}",
        u.path, u.params, urlencode(qs), u.fragment,
    ))


def _ensure_user_db(username: str) -> str | None:
    """Provision schema u_<user> + restricted role; return the user's
    DATABASE_URL (agent-only, scoped to their own schema) or None if DB is
    unavailable. Idempotent — safe to call on every login."""
    if not DB_URL or not _PSYCOPG2:
        return None
    safe = re.sub(r"[^A-Za-z0-9_]", "_", username)
    schema = f"u_{safe}"
    role = f"u_{safe}"

    # existing password (keep stable across logins) or create one
    row = _db_query("SELECT role_password FROM hermes_users WHERE username = %s", (username,))
    password = row[0][0] if row else secrets.token_urlsafe(24)

    conn = _db_connect()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                pgsql.SQL("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = %s) THEN CREATE ROLE {} LOGIN PASSWORD %s; END IF; END $$;")
                .format(pgsql.Identifier(role)), (role, password))
            cur.execute(pgsql.SQL("CREATE SCHEMA IF NOT EXISTS {} AUTHORIZATION {}")
                        .format(pgsql.Identifier(schema), pgsql.Identifier(role)))
            cur.execute(pgsql.SQL("GRANT USAGE, CREATE ON SCHEMA {} TO {}")
                        .format(pgsql.Identifier(schema), pgsql.Identifier(role)))
            cur.execute(pgsql.SQL("REVOKE ALL ON SCHEMA public FROM {}")
                        .format(pgsql.Identifier(role)))
            cur.execute(pgsql.SQL("ALTER ROLE {} SET search_path TO {}")
                        .format(pgsql.Identifier(role), pgsql.Identifier(schema)))
            cur.execute(pgsql.SQL(
                "CREATE TABLE IF NOT EXISTS {}.documents ("
                " id SERIAL PRIMARY KEY, filename TEXT NOT NULL, path TEXT, "
                " content TEXT, uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            ).format(pgsql.Identifier(schema)))
            cur.execute(pgsql.SQL("GRANT ALL ON {}.documents TO {}")
                        .format(pgsql.Identifier(schema), pgsql.Identifier(role)))
            cur.execute(pgsql.SQL(
                "CREATE TABLE IF NOT EXISTS {}.credentials ("
                " service TEXT PRIMARY KEY, payload TEXT NOT NULL, "
                " updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            ).format(pgsql.Identifier(schema)))
            cur.execute(pgsql.SQL("GRANT ALL ON {}.credentials TO {}")
                        .format(pgsql.Identifier(schema), pgsql.Identifier(role)))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[chatbot] WARNING: could not provision user db for {username}: {e}")
        return None

    _db_exec(
        "INSERT INTO hermes_users (username, schema_name, role_name, role_password) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (username) DO UPDATE SET role_password = EXCLUDED.role_password",
        (username, schema, role, password),
    )
    url = _restricted_url(username, role, password, schema)
    USER_DB[username] = url
    print(f"[chatbot] user {username}: schema {schema} ready (restricted role {role})")
    return url


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
def _save_upload(user: str, file_obj):
    """Save an uploaded file to uploads/<user>/; return the path, or None on reject."""
    if file_obj is None or not file_obj.filename:
        return None
    file_obj.stream.seek(0, os.SEEK_END)
    size = file_obj.stream.tell()
    file_obj.stream.seek(0)
    if size <= 0 or size > MAX_UPLOAD_MB * 1024 * 1024:
        return None
    safe_user = re.sub(r"[^A-Za-z0-9_.-]", "_", user)
    osuser = USER_OS.get(user)
    udir = os.path.join(f"/home/{osuser}", "uploads") if osuser else os.path.join(UPLOAD_ROOT, safe_user)
    os.makedirs(udir, exist_ok=True)
    dest = os.path.join(udir, f"{int(time.time() * 1000)}-{os.path.basename(file_obj.filename)}")
    file_obj.save(dest)
    print(f"[chatbot] saved upload: {dest} ({size} bytes)")

    # record in the user's own schema (per-user database) so the agent can
    # query it; text files get their content mirrored for easy grep
    content = None
    if dest.lower().endswith((".txt", ".md", ".csv", ".json")):
        try:
            with open(dest, encoding="utf-8", errors="ignore") as f:
                content = f.read()[:200000]
        except OSError:
            content = None
    _db_exec(
        f"INSERT INTO u_{safe_user}.documents (filename, path, content) VALUES (%s, %s, %s)",
        (os.path.basename(dest), dest, content),
    )
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


def _safe_os_user(username: str) -> str:
    return "u_" + re.sub(r"[^A-Za-z0-9_]", "_", username)[:24]


def _ensure_os_user(username: str) -> str | None:
    """Create an isolated Linux account (home 0700) so the agent process runs as
    that user and cannot read other users' data at the filesystem level.
    Returns the account name, or None when not possible (non-root / non-POSIX)
    — in that case the app falls back to running agents as the container user."""
    if os.name != "posix":
        return None
    try:
        if os.geteuid() != 0 or shutil.which("useradd") is None:
            return None
    except AttributeError:
        return None

    osuser = _safe_os_user(username)
    schema = f"u_{re.sub(r'[^A-Za-z0-9_]', '_', username)}"  # same schema as _ensure_user_db
    home = f"/home/{osuser}"
    if subprocess.run(["id", "-u", osuser], capture_output=True).returncode != 0:
        subprocess.run(["useradd", "-m", "-s", "/bin/bash", osuser], check=False)
    subprocess.run(["chmod", "700", home], check=False)

    hh = os.path.join(home, "hermes")
    for d in ("hermes", "uploads"):
        os.makedirs(os.path.join(home, d), exist_ok=True)
        subprocess.run(["chown", "-R", f"{osuser}:{osuser}", os.path.join(home, d)], check=False)

    # per-user hermes secrets (.env) + model config, so the user's agent works standalone
    env_lines = [f"{k}={os.environ[k]}" for k in _PROVIDER_ENV_KEYS if os.environ.get(k)]
    if env_lines:
        env_path = os.path.join(hh, ".env")
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("\n".join(env_lines) + "\n")
        subprocess.run(["chown", f"{osuser}:{osuser}", env_path], check=False)
    model = os.environ.get("RAILWAY_MODEL", "deepseek/deepseek-chat")
    hermes_bin = os.path.abspath(HERMES)
    if shutil.which("runuser"):
        subprocess.run(["runuser", "-u", osuser, "--", "env", f"HERMES_HOME={hh}",
                        hermes_bin, "config", "set", "model", model],
                       capture_output=True, text=True, check=False)

    # standing instructions for this user's agent (loaded from cwd by hermes)
    agents_md = f"""# SmartReviewAgent — EU technical-documentation review assistant

Your name is SmartReviewAgent. You guide the user to create technical
documentation that fulfills the EU regulations.[truncated]

1. Cross-check the user's instruction manual against the applicable EU regulations.
2. EU regulations live in the SHARED schema (readable by every user):
   psql "$DATABASE_URL" -c "SELECT title FROM shared.regulations;"
   Read the full texts, e.g.:
   psql "$DATABASE_URL" -c "SELECT title, LEFT(content, 500) FROM shared.regulations WHERE title ILIKE '%lvd%';"
3. The user's documents (instruction manual, contact info, uploads) are in YOUR schema:
   psql "$DATABASE_URL" -c "SELECT id, filename, path, uploaded_at FROM {schema}.documents ORDER BY uploaded_at DESC;"
   and as files in your home directory: /home/{osuser}/uploads/
4. When checking a manual: identify the applicable regulations (GPSR, LVD 2014/35/EU,
   PPWR, RoHS, WEEE, ...), then verify each required element: safety instructions and
   warnings, CE marking / EU Declaration of Conformity references, language coverage,
   manufacturer/importer identification, contact details, etc.
5. If any required part is MISSING or unclear: ASK THE USER for that specific
   information in the chat. Do NOT invent or guess it.
6. Report findings as a clear checklist: ✅ present / ❌ missing / ⚠️ unclear, each
   item citing the relevant regulation.
7. Gmail: if the user connected Gmail (✉️ button in the UI), you may read and send
   THIS user's own mail only:
     ACCESS=$(python3 /opt/hermes/gmail_token.py)
     curl -H "Authorization: Bearer $ACCESS" "https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults=5"
     curl -H "Authorization: Bearer $ACCESS" "https://gmail.googleapis.com/gmail/v1/users/me/messages/{{id}}"
   To send: POST /gmail/v1/users/me/messages with {{"raw": "<base64url of RFC822 message>"}}.
   Never access any other mailbox.
"""
    with open(os.path.join(home, "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write(agents_md)
    subprocess.run(["chown", f"{osuser}:{osuser}", os.path.join(home, "AGENTS.md")], check=False)

    USER_OS[username] = osuser
    print(f"[chatbot] user {username}: isolated OS account {osuser} (home {home}, 0700)")
    return osuser


# ---------------------------------------------------------------- Gmail OAuth
# One app-level OAuth client (your Google Cloud "Web application" client) serves
# ALL users: each user authorizes their OWN Gmail via the consent screen and gets
# their OWN refresh token, stored in their own schema. Users never handle clients.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
OAUTH_REDIRECT = os.environ.get("OAUTH_REDIRECT_URI", "").rstrip("/") or \
    f"https://{os.environ.get('PUBLIC_HOST', 'pergamon-production.up.railway.app')}/oauth2callback"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"

# one-time state -> username for the OAuth round-trip (expires in 10 min)
_OAUTH_STATES = {}
_OAUTH_STATES_LOCK = threading.Lock()


def _gmail_ready(username: str) -> bool:
    """Does this user already have a Gmail refresh token in their own schema?"""
    if not _PSYCOPG2:
        return False
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", username)
    try:
        return bool(_db_query(
            f"SELECT 1 FROM u_{safe}.credentials WHERE service = 'gmail'"))
    except Exception:
        return False


def _gmail_token_store(username: str, refresh_token: str) -> None:
    """Persist the user's refresh token into their own schema + their .env
    (chowned to their OS account so their agent can use it)."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", username)
    _db_exec(
        f"INSERT INTO u_{safe}.credentials (service, payload) VALUES ('gmail', %s) "
        "ON CONFLICT (service) DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()",
        (refresh_token,),
    )
    osuser = USER_OS.get(username)
    if osuser:
        env_path = f"/home/{osuser}/hermes/.env"
        try:
            lines = []
            if os.path.exists(env_path):
                with open(env_path, encoding="utf-8") as f:
                    lines = [l for l in f.read().splitlines()
                             if l and not l.startswith(("GOOGLE_REFRESH_TOKEN", "GOOGLE_OAUTH_CLIENT"))]
            lines += [f"GOOGLE_REFRESH_TOKEN={refresh_token}",
                      f"GOOGLE_OAUTH_CLIENT_ID={GOOGLE_CLIENT_ID}",
                      f"GOOGLE_OAUTH_CLIENT_SECRET={GOOGLE_CLIENT_SECRET}"]
            with open(env_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            subprocess.run(["chown", f"{osuser}:{osuser}", env_path], check=False)
        except OSError as e:
            print(f"[chatbot] WARNING: could not write gmail env for {username}: {e}")


def _oauth_consent_url(username: str) -> str:
    import urllib.parse
    state = secrets.token_urlsafe(16)
    with _OAUTH_STATES_LOCK:
        _OAUTH_STATES[state] = {"user": username, "created": time.time()}
    # persist so an app restart mid-flow doesn't invalidate the consent
    _db_exec("INSERT INTO oauth_states (state, username) VALUES (%s, %s) "
             "ON CONFLICT (state) DO NOTHING", (state, username))
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT,
        "response_type": "code",
        "scope": GMAIL_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return "https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode(params)


def _run_agent(chat_id: str, message: str, user: str = "") -> str:
    """Run one full agent turn, resuming the chat's session when one exists.
    The agent's DATABASE_URL is the user's RESTRICTED url (own schema only)."""
    run_env = dict(os.environ, HERMES_HOME=os.environ.get("HERMES_HOME", "/opt/data"))
    restricted = USER_DB.get(user)
    if restricted:
        run_env["DATABASE_URL"] = restricted  # per-user role: cannot see other users' data
    elif user:
        run_env.pop("DATABASE_URL", None)  # never hand the admin URL to the agent

    osuser = USER_OS.get(user)
    if osuser:
        run_env["HERMES_HOME"] = f"/home/{osuser}/hermes"  # per-user hermes state

    sid = SESSIONS.get(chat_id)
    cmd = [os.path.abspath(HERMES), "chat", "-Q"]
    if sid:
        cmd += ["--resume", sid]
    cmd += ["-q", message]

    if osuser and shutil.which("runuser"):
        # run as the user's isolated OS account — filesystem isolation is
        # enforced by the kernel (home dirs are 0700)
        agent_env = {"HERMES_HOME": run_env["HERMES_HOME"]}
        if "DATABASE_URL" in run_env:
            agent_env["DATABASE_URL"] = run_env["DATABASE_URL"]
        for k in _PROVIDER_ENV_KEYS:
            if os.environ.get(k):
                agent_env[k] = os.environ[k]
        prefix = ["runuser", "-u", osuser, "--", "env"] + [f"{k}={v}" for k, v in agent_env.items()]
        proc_cwd = f"/home/{osuser}"  # hermes loads AGENTS.md (standing instructions) from cwd
    else:
        prefix = None  # fallback: run as the container user (dev/testing)
        proc_cwd = None

    proc = subprocess.run(
        (prefix + cmd) if prefix else cmd, capture_output=True, text=True, timeout=TIMEOUT,
        env=run_env if prefix is None else None, cwd=proc_cwd,
    )

    # Resume can fail (e.g. session lost after a container restart) — retry fresh
    if sid and proc.returncode != 0:
        SESSIONS.pop(chat_id, None)
        _db_exec("DELETE FROM hermes_chats WHERE chat_id = %s", (chat_id,))
        fresh = [os.path.abspath(HERMES), "chat", "-Q", "-q", message]
        proc = subprocess.run(
            (prefix + fresh) if prefix else fresh, capture_output=True, text=True,
            timeout=TIMEOUT, env=run_env if prefix is None else None, cwd=proc_cwd,
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
    return Response(PAGE, mimetype="text/html")


@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if username in USERS and secrets.compare_digest(USERS[username], password):
        _ensure_user_db(username)   # per-user DB schema/role (best-effort)
        _ensure_os_user(username)   # isolated OS account (best-effort)
        return jsonify(token=_issue_token(username), user=username)
    return jsonify(error="unauthorized"), 401


@app.post("/api/chat")
def chat():
    user = _auth_user()
    if user is None:
        return jsonify(error="unauthorized"), 401

    # multipart (file upload or plain form from the chat UI) or JSON
    if request.mimetype == "multipart/form-data" or request.files:
        message = (request.form.get("message") or "").strip()
        client_chat_id = (request.form.get("chat_id") or "default")[:64]
        f = request.files.get("file")
        path = _save_upload(user, f)
        if path is None and f is not None:
            return jsonify(error="invalid or too-large file (max %dMB)" % MAX_UPLOAD_MB), 400
        if path:
            message = f"[User attached a file at: {path}]\n\n{message or 'Please read the attached file and summarize it.'}"
    else:
        data = request.get_json(silent=True) or {}
        message = (data.get("message") or "").strip()
        client_chat_id = (data.get("chat_id") or "default")[:64]

    if not message:
        return jsonify(error="empty message"), 400
    if len(message) > 8000:
        return jsonify(error="message too long"), 400

    # user-scoped isolation: hermes sessions, history and uploads are per-user
    chat_id = f"{user}:{client_chat_id}"

    _db_log_message(chat_id, "user", message)
    with _lock:  # one agent process at a time (prototype-grade)
        started = time.time()
        try:
            reply = _run_agent(chat_id, message, user)
        except subprocess.TimeoutExpired:
            return jsonify(error=f"agent timed out after {TIMEOUT}s"), 504
    _db_log_message(chat_id, "agent", reply)
    return jsonify(reply=reply, elapsed=round(time.time() - started, 1))


@app.post("/api/gmail/auth-url")
def gmail_auth_url():
    user = _auth_user()
    if user is None:
        return jsonify(error="unauthorized"), 401
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return jsonify(error="Gmail OAuth is not configured on the server yet (missing GOOGLE_OAUTH_CLIENT_ID/SECRET)"), 503
    return jsonify(url=_oauth_consent_url(user), redirect_uri=OAUTH_REDIRECT)


@app.get("/api/gmail/status")
def gmail_status():
    user = _auth_user()
    if user is None:
        return jsonify(error="unauthorized"), 401
    return jsonify(connected=_gmail_ready(user))


@app.get("/oauth2callback")
def oauth2callback():
    code = request.args.get("code", "")
    state = request.args.get("state", "")
    err = request.args.get("error", "")
    err_desc = request.args.get("error_description", "")
    if err:
        print(f"[chatbot] oauth rejected by Google: {err} {err_desc}")
        return jsonify(error=f"oauth failed: google error={err} {err_desc}".strip()), 400

    # state lookup: in-memory first, then DB (survives app restarts)
    with _OAUTH_STATES_LOCK:
        entry = _OAUTH_STATES.pop(state, None)
    username = None
    if entry:
        username = entry["user"]
    else:
        rows = _db_query("SELECT username, created_at FROM oauth_states WHERE state = %s", (state,))
        _db_exec("DELETE FROM oauth_states WHERE state = %s", (state,))
        if rows:
            age = time.time() - rows[0][1].timestamp()
            if age > 600:
                return jsonify(error="oauth state expired — click ✉️ again"), 400
            username = rows[0][0]
    if not username:
        print(f"[chatbot] oauth state not found (stale consent URL?) state={state[:8]}…")
        return jsonify(error="oauth failed: state not found (consent window too old) — click ✉️ again"), 400
    if not code:
        return jsonify(error="oauth failed: no authorization code"), 400

    import urllib.parse
    data = urllib.parse.urlencode({
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": OAUTH_REDIRECT,
        "grant_type": "authorization_code",
    }).encode()
    try:
        with urllib.request.urlopen(
            urllib.request.Request("https://oauth2.googleapis.com/token", data=data,
                                   headers={"Content-Type": "application/x-www-form-urlencoded"}),
            timeout=20,
        ) as resp:
            tok = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")[:300]
        print(f"[chatbot] oauth token exchange failed: HTTP {e.code} {detail}")
        return jsonify(error=f"token exchange failed: HTTP {e.code} {detail}"), 502
    except Exception as e:
        print(f"[chatbot] oauth token exchange failed: {e}")
        return jsonify(error=f"token exchange failed: {e}"), 502
    refresh = tok.get("refresh_token")
    if not refresh:
        return jsonify(error="no refresh token returned"), 400
    _gmail_token_store(entry["user"], refresh)
    print(f"[chatbot] gmail connected for user {entry['user']}")
    return redirect("/?gmail_connected=1")


ONBOARD_TEXT = (
    "Hi! I am SmartReviewAgent — an agent that guides you to make a technical "
    "documentation that fulfills the EU regulations.\n\n"
    "Here is what I can do for you:\n\n"
    "• I have an isolated database that stores all your personal documents — "
    "only you and I can access them.\n"
    "• You can upload your draft of your technical document anytime with the "
    "📎 button.\n"
    "• I will help you to review it and check if it satisfies the EU regulations.\n"
    "• If you have any uncertainty about some details, you can send me the Gmail "
    "of your technical team and I will email them for you.\n"
    "• Connect your own Gmail with the ✉️ button if you want me to send emails "
    "on your behalf.\n\n"
    "Upload your document now to get started!"
)


@app.post("/api/onboard")
def onboard():
    user = _auth_user()
    if user is None:
        return jsonify(error="unauthorized"), 401
    data = request.get_json(silent=True) or {}
    client_id = (data.get("chat_id") or "default")[:64]
    chat_id = f"{user}:{client_id}"
    _db_log_message(chat_id, "agent", ONBOARD_TEXT)
    return jsonify(reply=ONBOARD_TEXT)


@app.get("/api/sessions")
def sessions_list():
    user = _auth_user()
    if user is None:
        return jsonify(error="unauthorized"), 401
    rows = _db_query(
        "SELECT m.chat_id, count(*) AS n, max(m.created_at) AS last_at, "
        "(SELECT content FROM hermes_messages x WHERE x.chat_id = m.chat_id "
        " ORDER BY x.id DESC LIMIT 1) AS last_msg "
        "FROM hermes_messages m WHERE m.chat_id LIKE %s "
        "GROUP BY m.chat_id ORDER BY last_at DESC NULLS LAST",
        (f"{user}:%",),
    )
    out = []
    for chat_id, n, last_at, last_msg in rows:
        client_id = chat_id.split(":", 1)[1] if ":" in chat_id else chat_id
        snippet = (last_msg or "").strip().splitlines()[0][:40] if last_msg else ""
        out.append({
            "id": client_id, "count": n,
            "last_at": last_at.isoformat() if last_at else None,
            "title": snippet or "Session",
        })
    return jsonify(sessions=out)


@app.get("/api/messages")
def messages_list():
    user = _auth_user()
    if user is None:
        return jsonify(error="unauthorized"), 401
    client_id = (request.args.get("chat_id") or "default")[:64]
    rows = _db_query(
        "SELECT role, content FROM hermes_messages WHERE chat_id = %s "
        "ORDER BY id DESC LIMIT 60",
        (f"{user}:{client_id}",),
    )
    return jsonify(messages=[{"role": r[0], "content": r[1]} for r in reversed(rows)])


if __name__ == "__main__":
    _db_init()
    _db_load_sessions()
    print(f"[chatbot] users configured: {', '.join(USERS)}")
    print(f"[chatbot] surface ready (hermes={HERMES}, token_ttl={TOKEN_TTL_HOURS}h)")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "3000")), threaded=True)
