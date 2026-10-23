# ngrok-chat

A small self-hosted PWA chat app: Flask + SQLite + Web Push, designed to be
published through a tunnel (cloudflared or ngrok) from a loopback port.

* **App**: Flask (`app.py`), SSE for real-time messages, Web Push (VAPID) for
  notifications, SQLite for storage.
* **Data lives on this VPS** in `data/chat.db` (WAL mode) plus
  `data/vapid_private.pem` and push subscriptions. Nothing is sent anywhere
  else — the tunnel only forwards HTTP to the app on loopback.
* **Default bind is `127.0.0.1:8025`**, so the app is only reachable through a
  tunnel (or if you deliberately set `CHAT_HOST=0.0.0.0` for LAN access).

---

## Quick start

```bash
sudo systemctl start ngrok-chat.service   # PRODUCTION: app only, no tunnel needed
sudo systemctl status ngrok-chat.service  # (tunnel = romulator2k-ngrok.service -> /chat/)

./start.sh --foreground --tunnel=none     # DEBUGGING: run attached to your terminal
./start.sh status                         # health of app + tunnel, log/pid/db paths
./start.sh stop                           # stop an app/tunnel THIS script started
```

**Production layout (what's running now):**

```
public: https://uncolored-epidermis-saved.ngrok-free.dev
   |      romulator2k-ngrok.service (ngrok, do not stop)
   v
nginx 127.0.0.1:8011        <-- /chat/  -> 127.0.0.1:8025 (ngrok-chat.service)
   |                            /       -> 127.0.0.1:8010 (ROMulator2K backend)
   v
chat app (gunicorn, 1 worker) on 127.0.0.1:8025, SQLite in data/chat.db
```

The chat and ROMulator2K **fail independently**: nginx routes `/chat/` to a
different upstream (`:8025`) than everything else (`:8010`), so if the
ROMulator2K process dies the chat still works, and vice versa. Only an nginx
or tunnel outage takes down both.

`start.sh` is the **development/debugging** launcher — in production the app
is `ngrok-chat.service` and the tunnel is `romulator2k-ngrok.service`, both
enabled at boot. `start.sh` defaults to `--tunnel=none` because the real
public URL is already served by that systemd tunnel; starting another would
be a competing front door.

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

### Subcommands

| Command         | Effect |
|-----------------|--------|
| `./start.sh`    | Start app (gunicorn daemon) + tunnel (default `none` — no tunnel, see Quick start). |
| `./start.sh stop`   | Stop gunicorn via `run/gunicorn.pid` and the tunnel via `run/tunnel.pid`. Only ever kills processes this script started — never the other services on this box, and never the systemd `ngrok-chat.service`. |
| `./start.sh status` | Report gunicorn/tunnel state, print `/api/health`, DB path/size. Exits non-zero if unhealthy (cron/monitoring friendly). |

### Tunnel selection: `--tunnel=cloudflared|ngrok|none`

**Default is `none`, and that is correct for production** — the public URL
already exists via `romulator2k-ngrok.service` → nginx `:8011` → `/chat/`.
Only reach for the options below if you want an *additional* tunnel for
development.

* **`none` (default)** — app only, local on `127.0.0.1:8025`.
* **`cloudflared`** — extra ephemeral quick tunnel
  (`cloudflared tunnel --url http://127.0.0.1:8025 --no-autoupdate`). Zero
  conflicts with anything else on this box. **Caveat: the URL changes on every
  restart** (see "Web Push caveats").
* **`ngrok`** — needs a *free* ngrok account that is not already in use here.
  See **The ngrok reality** below before trying this; a bare `ngrok http`
  on this box fails with `ERR_NGROK_334`. `start.sh` refuses to run
  `--tunnel=ngrok` without `--authtoken` or `--config` (the default config's
  account is occupied, so trying it would only produce `ERR_NGROK_334`). If
  you pass `--authtoken`, a
  dedicated config is written to `run/ngrok-chat-*.yml` with a non-default
  local API (`web_addr: 127.0.0.1:4055`) so it can't collide with the other
  ngrok agents' APIs on `:4040`/`:4041`. If ngrok logs `ERR_NGROK_334`
  ("domain already online"), the launcher prints an explanation of the
  account situation instead of silently spinning.
* **`none`** — run the app with no public URL. Prints the local URL.

---

## Environment variables

All optional; set them in the shell before `./start.sh`, or via `Environment=`
lines in the systemd unit.

| Variable | Default | Meaning |
|---|---|---|
| `CHAT_PORT` | `8025` | TCP port the app binds. |
| `CHAT_HOST` | `127.0.0.1` | Bind address. Keep it on loopback unless you intentionally want LAN access. |
| `CHAT_ACCESS_CODE` | *(empty)* | Shared gate. When non-empty, **every API route** requires this code. Leave empty for an open chat. |
| `CHAT_DATA_DIR` | `<project>/data` | Where `chat.db`, `vapid_private.pem` and push-subscription state live. |
| `CHAT_VAPID_SUBJECT` | `mailto:admin@localhost` | Contact identity embedded in the VAPID JWT (`mailto:` or `https:`). Use a real address if you run this in production. |
| `CHAT_VAPID_PRIVATE_KEY` | *(unset)* | Raw PEM/base64 VAPID private key override. By default the key is read from / generated at `data/vapid_private.pem`. |

---

## The ngrok reality (read this before using `--tunnel=ngrok`)

This VPS has exactly **two** ngrok accounts and **both are occupied** by other
services that must **not** be stopped:

| Account | Config | Held by | Public URL → local |
|---|---|---|---|
| A (the default ngrok config `~/.config/ngrok/ngrok.yml`) | default | `orbit-ngrok@debian.service` — a protected, immutable (`chattr +i`), watchdog-restarted systemd unit | `https://sulphuric-shortly-define.ngrok-free.dev` → `127.0.0.1:8099` (nginx proxy: orbit chat + noVNC desktops) |
| B | `/home/debian/projects/ROMulator2K/ngrok.yml` | `romulator2k-ngrok.service` | `https://uncolored-epidermis-saved.ngrok-free.dev` → `127.0.0.1:8011` (nginx: `/chat/` → `:8025` chat, everything else → `:8010` ROMulator2K) |

Free ngrok allows **one online agent per account**. So running
`ngrok http 8025` (or any additional ngrok) against either account fails with
**`ERR_NGROK_334` — "domain already online"**. `start.sh` detects this and
prints the explanation instead of silently spinning. **Do not stop
`orbit-ngrok@debian.service` or `romulator2k-ngrok.service` to "make room".**

Your options, in order of preference:

1. **Already done — use the existing URL.** The chat is served at
   `https://uncolored-epidermis-saved.ngrok-free.dev/chat/` through an nginx
   proxy on `127.0.0.1:8011` that `romulator2k-ngrok.service` points at. No
   extra tunnel, no new account, and a **stable domain** (see "Web Push
   caveats" — this matters for push subscriptions).
2. **A separate ephemeral tunnel** (dev/testing): `./start.sh --tunnel=cloudflared`.
   No conflicts, no accounts, ephemeral URL.
3. **Supply a fresh ngrok authtoken** — create a *third* ngrok account, claim
   a domain on it, and run:
   ```bash
   ./start.sh --tunnel=ngrok --authtoken <your-new-token>
   ```
   This writes a dedicated config (local API on `127.0.0.1:4055`, tunnel named
   `ngrok-chat` → `http://127.0.0.1:8025`) and never touches the two existing
   accounts' configs.

#### How `/chat/` sharing works (already built and tested)

The nginx site `/etc/nginx/sites-available/romulator2k-chat-proxy` splits one
domain across two independent upstreams:

* `location /chat/ { proxy_pass http://127.0.0.1:8025/; }` — the **trailing
  slash strips the prefix**, so the app always thinks it is at `/` and needs
  no knowledge of the prefix. It also sets `X-Forwarded-Prefix: /chat`, which
  the app uses to emit `<base href="/chat/">`.
* `location / { proxy_pass http://127.0.0.1:8010; }` — ROMulator2K, untouched.

Because both are on the **same origin**, the app is explicitly *prefix-aware*
so it never bleeds into ROMulator2K's routes:

| Piece | How it stays under `/chat/` |
|---|---|
| API calls | `at()`/`BASE` in `static/app.js` prefixes every fetch |
| HTML assets | `<base href="{{ BASE }}/">` pins relative URLs to the mount |
| Service worker | registered at `BASE`, so scope is `/chat/` — it **cannot** intercept ROMulator2K's `/` |
| Manifest | `start_url`/`scope` are `./` (relative to the manifest) |
| Push click-through | `sw.js` resolves `#room` against its scope → lands on `/chat/#room` |

**ROMulator2K's own responses are byte-identical** through the proxy
(md5 `4fe007d9…` both before and after the switch), so nothing about their
app changed.

Rollback, if ever needed:
```bash
sudo cp /tmp/romulator2k-ngrok.service.bak /etc/systemd/system/romulator2k-ngrok.service
sudo systemctl daemon-reload && sudo systemctl restart romulator2k-ngrok.service
# and remove the site: sudo rm /etc/nginx/sites-enabled/romulator2k-chat-proxy && sudo systemctl reload nginx
```

---

## Why gunicorn runs ONE worker (do not "scale" it naively)

The Flask app keeps **SSE long-lived connections** and holds **in-memory
state**: an SSE `Bus` (per-room fan-out), a `CLIENTS` presence dict, and
rate-limit dicts. Each gunicorn worker is a separate process with its own
memory, so 2 workers would mean two `Bus`es and two presence tables —
messages posted to one worker wouldn't be pushed to SSE clients connected to
the other, and "who's online" would be wrong per-worker.

So the launcher and the systemd unit both use:

```text
gunicorn --workers 1 --worker-class gthread --threads 32 \
         --timeout 0 --keep-alive 30 \
         --bind 127.0.0.1:8025 app:app
```

* `-w 1` — one process, one set of in-memory state (correctness).
* `--threads 32` — concurrency comes from threads; `gthread` is the matching
  worker class (gunicorn switches to it automatically when `--threads > 1`,
  but we are explicit).
* `--timeout 0` — SSE watchers hold connections open indefinitely; the default
  30 s worker timeout would kill them.
* `--keep-alive 30` — for the tunnel/proxy in front.

Logs: `logs/gunicorn.log` (errors/app output), `logs/gunicorn-access.log`,
`logs/tunnel.log`. PID files: `run/gunicorn.pid`, `run/tunnel.pid`.

---

## Web Push caveats

* **VAPID keys** live in `data/vapid_private.pem` and are stable on disk (back
  them up with the DB — they're the app's push identity).
* **A push subscription is bound to the ORIGIN** (`https://host:port`) it was
  created on. **This is why the stable ngrok domain matters**: the production
  URL (`uncolored-epidermis-saved.ngrok-free.dev`) does **not** change across
  restarts, so push subscriptions **survive** reboots of the app, nginx and the
  tunnel. Nothing to do after a restart.
  * The one exception: if you use `./start.sh --tunnel=cloudflared` (an extra
    dev tunnel), *that* URL changes on every restart, so users on it must open
    the app once from the new origin to re-subscribe. Platform constraint, not
    a bug.
* **iOS/iPadOS Safari**: web push only works for sites added to the Home
  Screen ("Add to Home Screen"). Even then, push requires the app to have
  been opened at least once from the Home Screen icon.
* **ngrok free tier**: visitors first see ngrok's interstitial warning page.
  The service worker (and therefore push) can't activate until the visitor
  clicks through it.
* **Notifications are suppressed when the app is focused**: `sw.js` checks
  `clients.matchAll({includeUncontrolled:true})` and skips `showNotification`
  if any window has focus, so an open-and-visible tab never double-notifies.
  The service worker is the **only** place notifications are created.

---

## Installing the PWA

* **Chrome / Android**: open the URL → the install (monitor/download) icon
  appears in the address bar, or menu → *Install app* / *Add to Home screen*.
* **iOS Safari**: open the URL → Share button → **Add to Home Screen**. Push
  notifications only work in this installed mode (see above).
* The app is fully functional in a normal browser tab too; only iOS push
  requires installation.

---

## Data & backups

* DB: `data/chat.db` (SQLite, **WAL mode**) — messages, rooms, push
  subscriptions. `data/vapid_private.pem` is the VAPID private key.
* Because of WAL, a naive `cp` of `chat.db` can miss committed data still in
  `chat.db-wal`. `scripts/backup.sh` uses Python's `sqlite3` online backup API
  (via `./.venv/bin/python`; the `sqlite3` CLI is not installed on this box),
  which is consistent even while the app is serving traffic:

  ```bash
  ./scripts/backup.sh            # writes backups/chat-YYYYmmdd-HHMMSS.db, keeps newest 14
  ./scripts/backup.sh --keep 30  # keep 30 instead
  ```

* Scheduled on this box by **`ngrok-chat-backup.timer`** — daily at **04:30**,
  running `scripts/backup.sh` as `User=debian` via `ngrok-chat-backup.service`
  (see the **systemd** section). The script is cron-safe anyway (no prompts,
  non-zero exit on failure), so a plain crontab line works too if you'd rather
  not use systemd:

  ```cron
  17 4 * * * cd /home/debian/projects/ngrok-chat && ./scripts/backup.sh >> logs/backup.log 2>&1
  ```

* Rotation keeps the newest N snapshots (default 14) in `backups/`, deletes
  older `chat-*.db` files. Back up `data/vapid_private.pem` too (or copy the
  whole `data/` dir) so push identity survives a restore.

* **Logs** (`logs/gunicorn.log`, `logs/gunicorn-access.log`) are rotated daily
  by **`/etc/logrotate.d/ngrok-chat`** (repo copy `deploy/logrotate-ngrok-chat`):
  keeps 14 days, gzip-compresses with a one-day delay, and uses `copytruncate`
  so gunicorn keeps writing to the same file without a restart.

---

## Security notes

* **Loopback by default.** The app binds `127.0.0.1`; the only way in is
  through the tunnel (or LAN if you explicitly set `CHAT_HOST=0.0.0.0`).
* **Optional shared gate.** Set `CHAT_ACCESS_CODE` to require a single shared
  code on every API route. It is a blunt instrument (one code for everyone),
  not per-user auth.
* **XSS.** Messages are stored raw and rendered client-side with
  `textContent` (never `innerHTML`), so stored HTML/JS in messages cannot
  execute.
* **Nicknames are NOT identity.** Anyone with the link can post as any nick.
  Treat nicks as flavor text, not accounts. Do not rely on them for anything
  that matters.
* Anyone who gets the public URL (cloudflared/ngrok URLs are guessable-ish and
  shareable; ngrok free URLs are visible in ngrok's page) can reach the app —
  use `CHAT_ACCESS_CODE` if that matters to you.

---

## systemd (this box: INSTALLED)

The **app** runs as `ngrok-chat.service` — it is installed, enabled at boot,
and currently `active`. The unit in `deploy/ngrok-chat.service` runs the
**app only**; the tunnel is deliberately separate (`romulator2k-ngrok.service`)
so the two can restart independently — a tunnel restart shouldn't take the
chat DB process down with it.

```bash
sudo systemctl status   ngrok-chat.service
sudo systemctl restart  ngrok-chat.service   # after changing code/env
sudo journalctl -u ngrok-chat.service -f    # live logs (also logs/gunicorn.log)
```

`User=debian` ships **already set in the repo copy**
(`deploy/ngrok-chat.service`) — the unit is valid exactly as checked in, with
nothing to substitute before systemd will load it (an older revision of this
doc claimed the repo copy needed a value filled in; that is no longer true).
The very same file is what's installed and enabled on this box as
`ngrok-chat.service`.

### Scheduled backups: `ngrok-chat-backup.timer` (installed)

Backups no longer depend on a human remembering to run them. A unit pair
ships in `deploy/` and is installed + enabled on this box:

| Unit | Repo copy | What it does |
|---|---|---|
| `ngrok-chat-backup.timer` | `deploy/ngrok-chat-backup.timer` | Fires **daily at 04:30** (`OnCalendar=*-*-* 04:30:00`, `Persistent=true` — a missed run fires on next boot). Deliberately offset from `mcserver-backup.timer`, which owns 04:00. |
| `ngrok-chat-backup.service` | `deploy/ngrok-chat-backup.service` | `Type=oneshot`, `User=debian`, `ExecStart=scripts/backup.sh` — keeps the newest **14** snapshots. |

```bash
systemctl list-timers | grep ngrok-chat-backup   # next-run time
sudo systemctl start  ngrok-chat-backup.service  # run one right now
journalctl -u ngrok-chat-backup.service          # "snapshot ok: messages=N" / "wrote backups/..."
```

Install/enable from scratch (already done on this box):

```bash
sudo cp deploy/ngrok-chat-backup.timer deploy/ngrok-chat-backup.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ngrok-chat-backup.timer
```

### Log rotation: `/etc/logrotate.d/ngrok-chat` (installed)

The gunicorn logs (`logs/gunicorn*.log`) grow without bound otherwise. The
config ships as `deploy/logrotate-ngrok-chat` and is installed as
`/etc/logrotate.d/ngrok-chat` (the `logrotate` package on this box rotates it
daily):

| Setting | Value |
|---|---|
| Pattern | `/home/debian/projects/ngrok-chat/logs/gunicorn*.log` |
| Schedule / retention | `daily`, `rotate 14`, `compress` + `delaycompress` |
| Skips | `missingok` (files may not exist), `notifempty` (no empty rotations) |
| Ownership | `su debian debian` (the `logs/` dir is 0775 `debian:debian`), `create 0644 debian debian` |
| Method | **`copytruncate`** — gunicorn holds the log fd open, so a plain rename would leave it appending to the orphaned inode while the new file stayed empty. No `postrotate` and no service restart. |

```bash
sudo logrotate -d /etc/logrotate.d/ngrok-chat   # dry run — must report no errors
sudo logrotate -f /etc/logrotate.d/ngrok-chat   # force one now (leaves gunicorn*.log.1)
```

**Ownership rule: one owner at a time.** systemd owns `:8025` in production.
If you instead want to run under `./start.sh` (foreground debugging), stop
systemd first:

```bash
sudo systemctl stop ngrok-chat.service
./start.sh --foreground --tunnel=none
```

Otherwise gunicorn fails with `address already in use`, because both try to
bind `127.0.0.1:8025`. Use `systemctl status ngrok-chat` and
`./start.sh status` to see who is running what.

---

## Housekeeping rules on this box

* `start.sh` never runs `sudo`, never edits or touches systemd units other
  than the optional `deploy/ngrok-chat.service` (which it does not install),
  and never kills a process it did not start. In particular it will never
  touch `orbit-ngrok@debian.service`, `romulator2k-ngrok.service`, nginx, or
  the cloudflared noVNC services.
* `run/`, `logs/`, `backups/` are launcher-created working dirs (`backups/*.db`
  is already covered by the `*.db` gitignore rule; add `run/` + `logs/` to
  `.gitignore` if you version this repo).
