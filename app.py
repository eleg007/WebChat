#!/usr/bin/env python3
"""
ngrok-chat - a small self-hosted chat app built to be published through ngrok.

Design notes
------------
* Data lives in a local SQLite file on this machine (``data/chat.db``). Nothing
  is sent anywhere else - ngrok only tunnels HTTP to the app on loopback.
* The app binds to 127.0.0.1 by default so the only way in is through ngrok.
  Set CHAT_HOST=0.0.0.0 if you also want it reachable on the LAN.
* ``ProxyFix`` makes the app honour the ``X-Forwarded-Proto`` / ``X-Forwarded-Host``
  headers ngrok sets, so anything that builds an absolute URL uses the public
  https address rather than the loopback one.
* Real-time updates use Server-Sent Events. SSE is plain HTTP, so it works
  reliably through ngrok's free tier (no WebSocket upgrade to worry about) and
  the browser auto-reconnects on its own.
"""

from __future__ import annotations

import json
import os
import queue
import re
import sqlite3
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid01, b64urlencode
from pywebpush import WebPushException, webpush

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CHAT_DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "chat.db"

HOST = os.environ.get("CHAT_HOST", "127.0.0.1")
PORT = int(os.environ.get("CHAT_PORT", "8025"))

# Optional shared gate. If set, every API call must present this code, which
# keeps a public ngrok link from being wide open. Leave blank to disable.
ACCESS_CODE = os.environ.get("CHAT_ACCESS_CODE", "").strip()

MAX_BODY = 2000
MAX_NICK = 24
MAX_ROOM = 32
HISTORY_DEFAULT = 50
HISTORY_MAX = 200

# Rate limits (generous for humans, hostile to scripts).
MIN_INTERVAL = 0.7          # seconds between sends per client
WINDOW_SECS = 30
WINDOW_MAX = 25             # max sends per WINDOW_SECS per client

PRESENCE_TIMEOUT = 90       # drop a client after this long without a heartbeat

# Web Push: the VAPID signing key lives in data/ (gitignored) and is generated
# on first run. CHAT_VAPID_PRIVATE_KEY overrides it with a raw PEM or base64
# key; CHAT_VAPID_SUBJECT is the "mailto:"/"https:" contact in the VAPID JWT.
VAPID_SUBJECT = (os.environ.get("CHAT_VAPID_SUBJECT", "mailto:admin@localhost")
                 .strip() or "mailto:admin@localhost")
VAPID_KEY_PATH = DATA_DIR / "vapid_private.pem"

app = Flask(__name__)
# Trust one proxy hop (ngrok) so request.url_root and friends are correct.
# x_prefix lets ProxyFix honour X-Forwarded-Prefix when present.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.config["JSON_SORT_KEYS"] = False

DATA_DIR.mkdir(parents=True, exist_ok=True)


def base_path() -> str:
    """Mount point for the app in the browser's URL space.

    nginx can share this app under a prefix (e.g. /chat/) by stripping that
    prefix before proxying here, so the app itself always thinks it is at
    root. The only caller that knows the real external prefix is the proxy,
    so honour X-Forwarded-Prefix; otherwise fall back to the SCRIPT_NAME
    that a WSGI mount would set; otherwise root.

    Returns "" for a root mount and "/chat" (no trailing slash) for a prefix
    mount. Used to emit a <base href> so relative URLs (static assets, the
    manifest) resolve against the app's actual mount point rather than the
    document directory — at /chat the document directory is /, which would
    otherwise leak those requests to whatever else shares the origin.
    """
    prefix = (request.headers.get("X-Forwarded-Prefix", "").strip()
              or request.script_root.strip())
    return "/" + prefix.strip("/") if prefix.strip("/") else ""


@app.context_processor
def inject_base():
    return {"BASE": base_path()}


@app.route("/")
def index():
    # BASE reaches the template via the inject_base context processor.
    return render_template("index.html")

# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

SCHEMA = """
-- Core chat tables (always present)
CREATE TABLE IF NOT EXISTS rooms (
    name TEXT PRIMARY KEY,
    created TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    room    TEXT NOT NULL,
    nick    TEXT NOT NULL,
    user_id INTEGER REFERENCES users(id),   -- nullable: legacy msgs have no account
    body    TEXT NOT NULL,
    kind    TEXT NOT NULL DEFAULT 'msg',    -- 'msg' | 'system'
    created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_room_id ON messages (room, id);

-- Push subscriptions (pre-accounts, keyed by nick; rekeyed to user_id later)
CREATE TABLE IF NOT EXISTS push_subs (
    endpoint   TEXT PRIMARY KEY,
    p256dh     TEXT NOT NULL,
    auth       TEXT NOT NULL,
    nick       TEXT,
    preview    INTEGER NOT NULL DEFAULT 1,
    room       TEXT,
    created    TEXT NOT NULL,
    last_seen  TEXT
);
CREATE INDEX IF NOT EXISTS idx_push_subs_nick ON push_subs (nick);

-- Account tables (added in v2)
CREATE TABLE IF NOT EXISTS users (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    username  TEXT NOT NULL UNIQUE,          -- case-insensitive unique
    pass_hash TEXT NOT NULL,
    nick      TEXT NOT NULL,                 -- default display nickname
    created   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users (lower(username));

CREATE TABLE IF NOT EXISTS sessions (
    id        TEXT PRIMARY KEY,              -- URL-safe token (itsdangerous)
    user_id   INTEGER NOT NULL REFERENCES users(id),
    created   TEXT NOT NULL,
    expires   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id);
"""

# --------------------------------------------------------------------------
# Password hashing (argon2-cffi)
# --------------------------------------------------------------------------
from argon2 import PasswordHasher

ph = PasswordHasher()

# --------------------------------------------------------------------------
# Session cookie helpers
# --------------------------------------------------------------------------
import itsdangerous.url_safe as itsdangerous

# --------------------------------------------------------------------------
# Session cookie helpers
# --------------------------------------------------------------------------
from itsdangerous import URLSafeTimedSerializer

itsdangerous_signer = URLSafeTimedSerializer("ngrok-chat-session", salt="session")

SESSION_COOKIE = "chat_session"
SESSION_TTL_SEC = 60 * 60 * 24 * 14        # 14 days

def make_session_token(user_id: int) -> str:
    """Create a signed, URL-safe session token."""
    payload = {"uid": user_id}
    return itsdangerous_signer.dumps(payload)

def parse_session_token(token: str) -> int | None:
    """Verify and return user_id, or None if invalid/expired."""
    try:
        data = itsdangerous_signer.loads(token, max_age=SESSION_TTL_SEC)
        return int(data["uid"])
    except Exception:
        return None

def require_user(request: Request) -> dict | None:
    """Return user dict from session cookie, or None if not logged in."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    uid = parse_session_token(token)
    if not uid:
        return None
    with db() as c:
        row = c.execute("SELECT id, username, nick FROM users WHERE id = ?", (uid,)).fetchone()
    return {"id": row[0], "username": row[1], "nick": row[2]} if row else None

def require_login(user: dict | None) -> dict:
    """Raise 401 if no user."""
    if not user:
        raise HTTPException(401, "login required")
    return user

def require_admin(user: dict) -> dict:
    """Raise 403 if not the first user (admin)."""
    with db() as c:
        first = c.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
    if not first or user["id"] != first[0]:
        raise HTTPException(403, "admin only")
    return user


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@contextmanager
def db():
    """Short-lived connection per call - simple and safe with threads."""
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.executescript(SCHEMA)
        c.execute("INSERT OR IGNORE INTO rooms (name, created) VALUES (?, ?)",
                  ("general", now_iso()))


# --------------------------------------------------------------------------
# In-memory real-time state
# --------------------------------------------------------------------------

class Bus:
    """Fan-out of events to SSE subscribers, scoped to a room."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[str, list[queue.Queue]] = defaultdict(list)

    def subscribe(self, room: str) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._subs[room].append(q)
        return q

    def unsubscribe(self, room: str, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._subs[room].remove(q)
            except ValueError:
                pass

    def publish(self, room: str, event: str, data: dict) -> None:
        payload = _sse(event, data)
        with self._lock:
            targets = list(self._subs.get(room, ()))
        for q in targets:
            try:
                q.put_nowait(payload)
            except queue.Full:
                # A stuck client: drop the event rather than block everyone.
                pass


bus = Bus()

# client_id -> {"room", "nick", "last", "typing"}
CLIENTS: dict[str, dict] = {}
CLIENTS_LOCK = threading.Lock()

# client_id -> deque of send timestamps, for rate limiting
RATE: dict[str, deque] = defaultdict(deque)
RATE_LOCK = threading.Lock()


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def room_presence(room: str) -> dict:
    now = time.time()
    with CLIENTS_LOCK:
        people = {
            cid: info for cid, info in CLIENTS.items()
            if info["room"] == room and now - info["last"] < PRESENCE_TIMEOUT
        }
    nicks = sorted({info["nick"] for info in people.values()}, key=str.lower)
    typing = sorted(
        {info["nick"] for info in people.values()
         if now - info["typing"] < 5},
        key=str.lower,
    )
    return {"room": room, "people": nicks, "count": len(people), "typing": typing}


def _presence_worker() -> None:
    """Prune dead clients and broadcast presence for any room that changed."""
    last_sent: dict[str, tuple] = {}
    while True:
        time.sleep(3)
        now = time.time()
        with CLIENTS_LOCK:
            for cid in [c for c, i in CLIENTS.items()
                        if now - i["last"] > PRESENCE_TIMEOUT]:
                CLIENTS.pop(cid, None)
            rooms = {i["room"] for i in CLIENTS.values()}

        for room in rooms:
            snap = room_presence(room)
            key = (tuple(snap["people"]), tuple(snap["typing"]))
            if last_sent.get(room) != key:
                last_sent[room] = key
                bus.publish(room, "presence", snap)


threading.Thread(target=_presence_worker, daemon=True).start()

# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------

_ROOM_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def norm_room(raw) -> str | None:
    """Normalise a room name to a safe slug, or None if it can't be saved."""
    if not isinstance(raw, str):
        return None
    name = re.sub(r"[\s_]+", "-", raw.strip().lower())
    name = re.sub(r"[^a-z0-9-]", "", name)
    name = re.sub(r"-{2,}", "-", name).strip("-")
    return name if _ROOM_RE.match(name) else None


def norm_nick(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    nick = re.sub(r"[\x00-\x1f\x7f]", "", raw).strip()
    nick = re.sub(r"\s{2,}", " ", nick)
    return nick[:MAX_NICK] if 1 <= len(nick) <= MAX_NICK else None


def norm_body(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    body = raw.replace("\r\n", "\n").strip()
    if not body:
        return None
    return body[:MAX_BODY]


def client_key() -> str:
    """A stable-ish identity for rate limiting: real IP behind ngrok if present."""
    return request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()


def check_rate(key: str) -> tuple[bool, str]:
    now = time.time()
    with RATE_LOCK:
        hits = RATE[key]
        while hits and now - hits[0] > WINDOW_SECS:
            hits.popleft()
        if hits and now - hits[-1] < MIN_INTERVAL:
            return False, "Slow down a moment."
        if len(hits) >= WINDOW_MAX:
            return False, "Rate limit reached, try again shortly."
        hits.append(now)
    return True, ""


def msg_dict(row) -> dict:
    return {
        "id": row["id"],
        "room": row["room"],
        "nick": row["nick"],
        "user_id": row["user_id"],
        "body": row["body"],
        "kind": row["kind"],
        "ts": row["created"],
    }


# --------------------------------------------------------------------------
# Access gate
# --------------------------------------------------------------------------

def _presented_code() -> str:
    return (request.headers.get("X-Access-Code")
            or request.args.get("code")
            or "").strip()


def require_access():
    """Decorator: enforce the shared code when one is configured."""
    def deco(fn):
        from functools import wraps

        @wraps(fn)
        def wrapper(*args, **kwargs):
            if ACCESS_CODE and _presented_code() != ACCESS_CODE:
                return jsonify({"error": "access_code_required"}), 401
            return fn(*args, **kwargs)
        return wrapper
    return deco


# --------------------------------------------------------------------------
# Web Push: VAPID keys + fan-out
# --------------------------------------------------------------------------

_VAPID_LOCK = threading.Lock()
_vapid_state: dict[str, str] = {}   # {"pem": <PEM text>, "public": <urlsafe b64>}


def _write_vapid_key(pem: bytes) -> bool:
    """Create data/vapid_private.pem exclusively (mode 0600).

    Returns False if another process won the race, so the caller can read
    the winner's key instead of overwriting it.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(VAPID_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(pem)
        os.chmod(VAPID_KEY_PATH, 0o600)
        return True
    except OSError:
        try:
            VAPID_KEY_PATH.unlink()
        except OSError:
            pass
        raise


def _load_vapid() -> tuple[str, str]:
    """Return (private_pem, public_key_b64), generating the key on first run.

    Order: CHAT_VAPID_PRIVATE_KEY env override, then data/vapid_private.pem,
    then a fresh ECDSA P-256 (prime256v1) keypair written PEM, chmod 600.
    The public key is urlsafe base64 with the "=" padding stripped, which is
    what navigator.serviceWorker.pushManager.subscribe() expects.
    """
    if _vapid_state:
        return _vapid_state["pem"], _vapid_state["public"]
    with _VAPID_LOCK:
        if _vapid_state:                      # re-check inside the lock
            return _vapid_state["pem"], _vapid_state["public"]

        env_key = os.environ.get("CHAT_VAPID_PRIVATE_KEY", "").strip()
        if env_key:
            if "BEGIN" in env_key:
                vapid = Vapid01.from_pem(env_key.encode("utf-8"))
            else:
                vapid = Vapid01.from_string(env_key)   # base64 raw or DER
        elif VAPID_KEY_PATH.is_file():
            vapid = Vapid01.from_pem(VAPID_KEY_PATH.read_bytes())
        else:
            vapid = Vapid01()
            vapid.generate_keys()
            if not _write_vapid_key(vapid.private_pem()):
                vapid = Vapid01.from_pem(VAPID_KEY_PATH.read_bytes())

        if VAPID_KEY_PATH.is_file():
            try:
                os.chmod(VAPID_KEY_PATH, 0o600)
            except OSError:
                pass

        pub_raw = vapid.public_key.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        _vapid_state["pem"] = vapid.private_pem().decode("ascii")
        _vapid_state["public"] = b64urlencode(pub_raw)
        return _vapid_state["pem"], _vapid_state["public"]


_CTRL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_RUNS = re.compile(r"\s+")


def notify_body(preview: bool, body: str) -> str:
    """Sanitised, length-capped notification text (or the privacy placeholder).

    Control characters are stripped and whitespace collapsed so a hostile
    message cannot inject newlines/escape sequences into the payload, and the
    text is capped at 140 chars with an ellipsis when cut.
    """
    if not preview:
        return "Sent you a message"
    clean = _RUNS.sub(" ", _CTRL_CHARS.sub(" ", body)).strip()
    if len(clean) > 140:
        clean = clean[:139].rstrip() + "…"
    return clean or "Sent you a message"


def push_fanout(room: str, msg: dict) -> None:
    """Best-effort Web Push for one message, on a daemon thread.

    Never raises: a push-service failure must not affect the HTTP response or
    the stored message. Subscriptions that answer 404/410 are deleted.
    """
    try:
        with db() as c:
            rows = c.execute("SELECT * FROM push_subs").fetchall()
        if not rows:
            return
        try:
            pem, _ = _load_vapid()
        except Exception as exc:
            app.logger.warning("webpush skipped, VAPID key unavailable: %s", exc)
            return
        vapid_pem = pem.encode("utf-8")
        msg_id = msg.get("id")
        sender_user_id = msg.get("user_id")
        sender_nick = msg.get("nick")
        body = msg.get("body", "")
        now = now_iso()

        for r in rows:
            # Suppress by user_id if available, otherwise by nick
            if sender_user_id is not None and r["user_id"] is not None:
                if r["user_id"] == sender_user_id:
                    continue  # the sender's own devices
            elif sender_nick and (r["nick"] or "") == sender_nick:
                continue  # fallback: suppress by nick
            endpoint = r["endpoint"]
            try:
                payload = {
                    "title": f"{sender_nick} in #{room}",
                    "body": notify_body(bool(r["preview"]), body),
                    "tag": room,              # coalesce per-room
                    "renotify": True,
                    "icon": "/static/icons/icon-192.png",
                    "badge": "/static/icons/icon-192.png",
                    "data": {"url": f"/#{room}", "room": room, "id": msg_id},
                }
                webpush(
                    subscription_info={
                        "endpoint": endpoint,
                        "keys": {"p256dh": r["p256dh"], "auth": r["auth"]},
                    },
                    data=json.dumps(payload, ensure_ascii=False),
                    vapid_private_key=Vapid01.from_pem(vapid_pem),
                    vapid_claims={"sub": VAPID_SUBJECT},
                    timeout=10,
                )
                app.logger.debug("webpush ok -> %s", endpoint)
            except WebPushException as exc:
                status = exc.status_code
                if status in (404, 410):
                    # Endpoint gone or unsubscribed at the push service.
                    try:
                        with db() as c:
                            c.execute("DELETE FROM push_subs WHERE endpoint = ?",
                                      (endpoint,))
                        app.logger.warning("webpush: dropped dead subscription "
                                           "(%s) %s", status, endpoint)
                    except Exception as err:
                        app.logger.warning("webpush: could not drop "
                                           "subscription: %s", err)
                elif status == 429 or (status is not None and status >= 500):
                    app.logger.warning("webpush: transient %s from push "
                                       "service for %s", status, endpoint)
                else:
                    app.logger.warning("webpush failed for %s (%s): %s",
                                       endpoint, status, exc)
            except Exception as exc:
                app.logger.warning("webpush failed for %s: %s", endpoint, exc)

        # Record that these subscriptions were still live at send time.
        try:
            with db() as c:
                c.executemany(
                    "UPDATE push_subs SET last_seen = ? WHERE endpoint = ?",
                    [(now, r["endpoint"]) for r in rows],
                )
        except Exception as exc:
            app.logger.warning("webpush: last_seen update failed: %s", exc)
    except Exception:
        app.logger.exception("webpush fan-out failed")


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------

@app.get("/sw.js")
def service_worker():
    """Serve the service worker from the root scope (scope='/' requires it)."""
    resp = send_from_directory(os.path.join(BASE_DIR, "static"), "sw.js",
                               mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/manifest.webmanifest")
def web_manifest():
    resp = send_from_directory(os.path.join(BASE_DIR, "static"),
                               "manifest.webmanifest",
                               mimetype="application/manifest+json")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

@app.get("/api/config")
def api_config():
    return jsonify({
        "access_required": bool(ACCESS_CODE),
        "default_room": "general",
        "max_body": MAX_BODY,
        "max_nick": MAX_NICK,
    })


# --------------------------------------------------------------------------
# Auth API
# --------------------------------------------------------------------------

@app.post("/api/auth/register")
def api_auth_register():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""
    nick = norm_nick(data.get("nick")) or username

    if not username:
        return jsonify({"error": "bad_username"}), 400
    if len(username) > 32:
        return jsonify({"error": "bad_username"}), 400
    if username in ("system", "admin", "anonymous", "guest", "moderator", "root", "support"):
        return jsonify({"error": "bad_username"}), 400
    if not password or len(password) < 8:
        return jsonify({"error": "bad_password"}), 400

    # Check if already exists
    with db() as c:
        if c.execute("SELECT 1 FROM users WHERE lower(username) = ?", (username,)).fetchone():
            return jsonify({"error": "username_taken"}), 409

    pass_hash = ph.hash(password)
    now = now_iso()
    with db() as c:
        cur = c.execute(
            "INSERT INTO users (username, pass_hash, nick, created) VALUES (?, ?, ?, ?)",
            (username, pass_hash, nick, now),
        )
        uid = cur.lastrowid

    token = make_session_token(uid)
    resp = jsonify({"ok": True, "username": username, "nick": nick})
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="Lax", max_age=SESSION_TTL_SEC, path="/")
    return resp, 201


@app.post("/api/auth/login")
def api_auth_login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""

    if not username or not password:
        return jsonify({"error": "bad_credentials"}), 400

    with db() as c:
        row = c.execute("SELECT id, pass_hash, nick FROM users WHERE lower(username) = ?", (username,)).fetchone()
    if not row or not ph.verify(row["pass_hash"], password):
        return jsonify({"error": "bad_credentials"}), 401

    token = make_session_token(row["id"])
    resp = jsonify({"ok": True, "username": username, "nick": row["nick"]})
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="Lax", max_age=SESSION_TTL_SEC, path="/")
    return resp


@app.post("/api/auth/logout")
def api_auth_logout():
    resp = jsonify({"ok": True})
    resp.set_cookie(SESSION_COOKIE, "", expires=0, path="/")
    return resp


@app.get("/api/auth/me")
def api_auth_me():
    user = require_user(request)
    if not user:
        return jsonify({"authenticated": False})
    return jsonify({"authenticated": True, "username": user["username"], "nick": user["nick"]})


@app.get("/api/rooms")
@require_access()
def api_rooms():
    with db() as c:
        rows = c.execute(
            """
            SELECT r.name, r.created,
                   (SELECT COUNT(*) FROM messages m
                     WHERE m.room = r.name AND m.kind = 'msg') AS msgs,
                   (SELECT m.nick  FROM messages m
                     WHERE m.room = r.name ORDER BY m.id DESC LIMIT 1) AS last_nick,
                   (SELECT m.body  FROM messages m
                     WHERE m.room = r.name ORDER BY m.id DESC LIMIT 1) AS last_body,
                   (SELECT m.created FROM messages m
                     WHERE m.room = r.name ORDER BY m.id DESC LIMIT 1) AS last_ts
              FROM rooms r
             ORDER BY r.name
            """
        ).fetchall()
    out = []
    for r in rows:
        out.append({
            "name": r["name"],
            "created": r["created"],
            "msgs": r["msgs"],
            "last_nick": r["last_nick"],
            "last_body": r["last_body"],
            "last_ts": r["last_ts"],
            "online": room_presence(r["name"])["count"],
        })
    return jsonify({"rooms": out})


@app.post("/api/rooms")
@require_access()
def api_create_room():
    data = request.get_json(silent=True) or {}
    name = norm_room(data.get("name"))
    if not name:
        return jsonify({"error": "bad_room_name",
                        "hint": "1-32 chars: letters, digits, - or _"}), 400
    with db() as c:
        c.execute("INSERT OR IGNORE INTO rooms (name, created) VALUES (?, ?)",
                  (name, now_iso()))
    return jsonify({"room": name}), 201


@app.get("/api/messages")
@require_access()
def api_messages():
    room = norm_room(request.args.get("room")) or "general"
    try:
        limit = min(int(request.args.get("limit", HISTORY_DEFAULT)), HISTORY_MAX)
    except ValueError:
        limit = HISTORY_DEFAULT
    before = request.args.get("before_id", type=int)

    q = "SELECT * FROM messages WHERE room = ?"
    args: list = [room]
    if before:
        q += " AND id < ?"
        args.append(before)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)

    with db() as c:
        rows = c.execute(q, args).fetchall()
        exists = c.execute("SELECT 1 FROM rooms WHERE name = ?", (room,)).fetchone()

    msgs = [msg_dict(r) for r in reversed(rows)]
    oldest = msgs[0]["id"] if msgs else None
    more = len(rows) == limit
    return jsonify({"room": room, "messages": msgs, "oldest": oldest,
                    "more": more, "exists": bool(exists)})


@app.post("/api/messages")
@require_access()
def api_send():
    data = request.get_json(silent=True) or {}
    room = norm_room(data.get("room")) or "general"
    nick = norm_nick(data.get("nick"))
    body = norm_body(data.get("body"))

    if not nick:
        return jsonify({"error": "bad_nick"}), 400
    if not body:
        return jsonify({"error": "empty_body"}), 400

    ok, why = check_rate(client_key())
    if not ok:
        return jsonify({"error": "rate_limited", "message": why}), 429

    # Optional authenticated user
    user = require_user(request)

    with db() as c:
        c.execute("INSERT OR IGNORE INTO rooms (name, created) VALUES (?, ?)",
                  (room, now_iso()))
        cur = c.execute(
            "INSERT INTO messages (room, nick, user_id, body, kind, created) VALUES (?, ?, ?, ?, 'msg', ?)",
            (room, nick, user["id"] if user else None, body, now_iso()),
        )
        row = c.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()

    msg = msg_dict(row)
    bus.publish(room, "message", msg)
    # Fan out Web Push off-thread: it must never delay or fail the response.
    try:
        threading.Thread(target=push_fanout, args=(room, msg),
                         daemon=True, name="push-fanout").start()
    except Exception as exc:
        app.logger.warning("could not start push fan-out: %s", exc)
    return jsonify({"message": msg}), 201


@app.get("/api/stream")
@require_access()
def api_stream():
    room = norm_room(request.args.get("room")) or "general"
    nick = norm_nick(request.args.get("nick")) or "anon"
    cid = request.args.get("client_id") or uuid.uuid4().hex[:12]
    cid = re.sub(r"[^a-zA-Z0-9_-]", "", cid)[:32] or uuid.uuid4().hex[:12]

    with CLIENTS_LOCK:
        CLIENTS[cid] = {"room": room, "nick": nick,
                        "last": time.time(), "typing": 0.0}
    q = bus.subscribe(room)

    def gen():
        try:
            yield "retry: 2000\n\n"
            yield _sse("hello", {"client_id": cid, "room": room})
            yield _sse("presence", room_presence(room))
            while True:
                try:
                    item = q.get(timeout=20)
                    yield item
                except queue.Empty:
                    yield ": ping\n\n"   # keep-alive comment
        finally:
            bus.unsubscribe(room, q)
            with CLIENTS_LOCK:
                CLIENTS.pop(cid, None)
            bus.publish(room, "presence", room_presence(room))

    return Response(
        gen(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/api/ping")
@require_access()
def api_ping():
    data = request.get_json(silent=True) or {}
    cid = data.get("client_id")
    with CLIENTS_LOCK:
        if cid in CLIENTS:
            CLIENTS[cid]["last"] = time.time()
    return jsonify({"ok": True})


@app.post("/api/typing")
@require_access()
def api_typing():
    data = request.get_json(silent=True) or {}
    cid = data.get("client_id")
    with CLIENTS_LOCK:
        if cid in CLIENTS:
            CLIENTS[cid]["typing"] = time.time()
            CLIENTS[cid]["last"] = time.time()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Web Push API
# --------------------------------------------------------------------------

@app.get("/api/push/vapid-public-key")
def api_push_vapid_public_key():
    # Deliberately NOT behind require_access(): the browser needs this key
    # before it can build a subscription, i.e. before it has had any chance to
    # present the access code. A public key is not a secret.
    try:
        _, public = _load_vapid()
    except Exception as exc:
        app.logger.error("VAPID public key unavailable: %s", exc)
        return jsonify({"error": "vapid_unavailable"}), 500
    return jsonify({"public_key": public})


@app.post("/api/push/subscribe")
@require_access()
def api_push_subscribe():
    data = request.get_json(silent=True) or {}
    sub = data.get("subscription")
    if not isinstance(sub, dict):
        return jsonify({"error": "bad_subscription"}), 400
    endpoint = sub.get("endpoint")
    keys = sub.get("keys")
    if (not isinstance(endpoint, str)
            or not endpoint.startswith(("https://", "http://"))
            or len(endpoint) > 2000):
        return jsonify({"error": "bad_subscription"}), 400
    if not isinstance(keys, dict):
        return jsonify({"error": "bad_subscription"}), 400
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if (not isinstance(p256dh, str) or not p256dh or len(p256dh) > 256
            or not isinstance(auth, str) or not auth or len(auth) > 256):
        return jsonify({"error": "bad_subscription"}), 400

    user = require_user(request)
    nick = norm_nick(data.get("nick"))
    room = norm_room(data.get("room"))
    preview = 0 if data.get("preview") is False else 1
    now = now_iso()
    with db() as c:
        c.execute(
            """
            INSERT INTO push_subs (endpoint, p256dh, auth, nick, preview,
                                   room, created, last_seen, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET
                p256dh    = excluded.p256dh,
                auth      = excluded.auth,
                nick      = excluded.nick,
                preview   = excluded.preview,
                room      = excluded.room,
                last_seen = excluded.last_seen,
                user_id   = excluded.user_id
            """,
            (endpoint, p256dh, auth, nick, preview, room, now, now, user["id"] if user else None),
        )
    return jsonify({"ok": True}), 201


@app.post("/api/push/unsubscribe")
@require_access()
def api_push_unsubscribe():
    data = request.get_json(silent=True) or {}
    endpoint = data.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint:
        return jsonify({"error": "bad_endpoint"}), 400
    with db() as c:
        c.execute("DELETE FROM push_subs WHERE endpoint = ?", (endpoint,))
    return jsonify({"ok": True})          # idempotent


@app.get("/api/push/status")
@require_access()
def api_push_status():
    with db() as c:
        count = c.execute("SELECT COUNT(*) AS n FROM push_subs").fetchone()["n"]
    try:
        _load_vapid()
        configured = True
    except Exception as exc:
        app.logger.warning("VAPID not configured: %s", exc)
        configured = False
    return jsonify({"count": count, "vapid_configured": configured})


@app.get("/api/health")
def api_health():
    with db() as c:
        msgs = c.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
        rooms = c.execute("SELECT COUNT(*) AS n FROM rooms").fetchone()["n"]
    with CLIENTS_LOCK:
        online = len(CLIENTS)
    return jsonify({
        "ok": True,
        "db": str(DB_PATH),
        "messages": msgs,
        "rooms": rooms,
        "connected": online,
        "access_required": bool(ACCESS_CODE),
    })


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

# Initialize schema on import (gunicorn imports this module)
init_db()


if __name__ == "__main__":
    print(f"ngrok-chat  ->  http://{HOST}:{PORT}   (db: {DB_PATH})")
    app.run(host=HOST, port=PORT, threaded=True, debug=False)
