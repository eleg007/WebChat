# ngrok-chat — Components

Self-hosted PWA chat app: Flask backend under gunicorn, SQLite storage, web
push notifications, and optional systemd deployment units.

## systemd services

Unit files live in `deploy/` (not installed by default — see install notes in
each unit).

| Unit | Type | Purpose |
|---|---|---|
| `ngrok-chat.service` | simple | Runs the Flask app via gunicorn on `127.0.0.1:8025` — **app only, no tunnel**. Single worker (`--workers 1` required for in-memory SSE bus state), 32 threads, `--timeout 0` for long-lived SSE. Logs to `logs/gunicorn.log` + `logs/gunicorn-access.log`, PID to `run/gunicorn.pid`. `Restart=always`, graceful 60s stop. |
| `ngrok-chat-backup.service` | oneshot | Runs `scripts/backup.sh` — consistent SQLite online backup of `data/chat.db`. |
| `ngrok-chat-backup.timer` | timer | Triggers the backup daily at 04:30 (`Persistent=true`; deliberately avoids 04:00 which is taken by `mcserver-backup.timer`). |

Public tunnel (cloudflared / ngrok / nginx) is managed separately, e.g. via
`./start.sh --tunnel=...` or a cloudflared unit. Note: both ngrok accounts on
this host are occupied by other services — do not stop them.

## Libraries / dependencies

From `requirements.txt` (venv at `.venv/`):
- flask `>=3.0` — web framework
- pywebpush `>=2.0` — Web Push protocol
- py-vapid `>=1.9` — VAPID key signing for push
- gunicorn `>=22.0` — WSGI server
- Pillow `>=10.0` — image/icon generation

## Other components

- `app.py` — the Flask application (single module, ~36 KB).
- `start.sh` — dev/ops launcher that manages the app (and optionally a tunnel).
- `scripts/backup.sh` — SQLite WAL-safe backup script (used by the oneshot unit).
- `scripts/gen_icons.py` — PWA icon generator (uses Pillow).
- `templates/index.html` — chat UI template.
- `static/` — `app.js`, `style.css`, `sw.js` (service worker), `manifest.webmanifest`, `icons/`.
- `data/` — `chat.db`, `vapid_private.pem`, push subscriptions.
- `backups/` — SQLite backup snapshots.
- `logs/` — gunicorn logs.
- `run/` — PID file.
- `deploy/logrotate-ngrok-chat` — logrotate config.
- `.venv/` — Python virtual environment.
