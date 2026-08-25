#!/usr/bin/env python3
"""Print a Gmail access token for THIS user's agent.

Reads GOOGLE_REFRESH_TOKEN + OAuth client id/secret from the user's hermes
.env (written by the chatbot at "Connect Gmail" time), exchanges the refresh
token for a short-lived access token and prints it on stdout.

Usage (as the user's agent):
    ACCESS=$(python3 /opt/hermes/gmail_token.py)
    curl -H "Authorization: Bearer $ACCESS" \
         "https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults=5"
"""
import json
import os
import sys
import urllib.parse
import urllib.request


def _load_env(path):
    env = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k] = v
    return env


def main():
    home = os.environ.get("HERMES_HOME", "")
    env = _load_env(os.path.join(home, ".env"))
    refresh = env.get("GOOGLE_REFRESH_TOKEN") or os.environ.get("GOOGLE_REFRESH_TOKEN", "")
    cid = env.get("GOOGLE_OAUTH_CLIENT_ID") or os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    csec = env.get("GOOGLE_OAUTH_CLIENT_SECRET") or os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    if not (refresh and cid and csec):
        print("NO-GMAIL-TOKEN: user has not connected Gmail yet", file=sys.stderr)
        sys.exit(1)
    data = urllib.parse.urlencode({
        "client_id": cid,
        "client_secret": csec,
        "refresh_token": refresh,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        tok = json.loads(r.read().decode())
    print(tok["access_token"])


if __name__ == "__main__":
    main()
