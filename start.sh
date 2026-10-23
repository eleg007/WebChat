#!/usr/bin/env bash
#
# start.sh - launcher for ngrok-chat (Flask + SQLite + Web Push PWA chat).
#
# Runs the app under gunicorn (SINGLE worker - see README "Why one worker")
# and optionally puts a public tunnel in front of it.
#
# PRODUCTION: don't use this script. The app runs as ngrok-chat.service
#   (systemd) and the public tunnel is romulator2k-ngrok.service -> nginx :8011
#   -> /chat/. Both start on boot and are managed with:
#     sudo systemctl {start|stop|restart|status} ngrok-chat.service
#   See README "Running it".
#
# This script is the DEVELOPMENT / DEBUGGING launcher:
#   ./start.sh --tunnel=none                    # background, local only (default)
#   ./start.sh --foreground --tunnel=none       # run in the terminal (debugging)
#   ./start.sh stop                             # stop gunicorn + tunnel started by this script
#   ./start.sh status                           # health of gunicorn + tunnel
#
# NOTE the default tunnel is 'none' ON PURPOSE: the real public URL is served
# by romulator2k-ngrok.service (via nginx :8011), not by a tunnel this script
# starts. Starting one here would be a second, competing front door.
#
# To opt into an *additional* tunnel (useful if systemd's app unit is stopped):
#   ./start.sh --tunnel=cloudflared              # ephemeral quick-tunnel URL
#   ./start.sh --tunnel=ngrok --authtoken TOK    # ngrok with a FRESH authtoken (3rd account)
#   ./start.sh --tunnel=ngrok --config PATH      # ngrok config defining a 'ngrok-chat' tunnel
#
# Env overrides (all optional): CHAT_PORT CHAT_HOST CHAT_ACCESS_CODE
#   CHAT_DATA_DIR CHAT_VAPID_SUBJECT CHAT_VAPID_PRIVATE_KEY
#
# TROUBLESHOOTING: if gunicorn fails with "address already in use", the
# systemd unit already owns 127.0.0.1:8025 - run `sudo systemctl stop
# ngrok-chat.service` first (or use ./start.sh stop to stop a script-started one).
#
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---------------------------------------------------------------------------
# Layout & defaults
# ---------------------------------------------------------------------------
VENV_DIR="$SCRIPT_DIR/.venv"
GUNICORN="$VENV_DIR/bin/gunicorn"
RUN_DIR="$SCRIPT_DIR/run"
LOG_DIR="$SCRIPT_DIR/logs"

GUNICORN_PID_FILE="$RUN_DIR/gunicorn.pid"
TUNNEL_PID_FILE="$RUN_DIR/tunnel.pid"
TUNNEL_URL_FILE="$RUN_DIR/tunnel.url"
TUNNEL_KIND_FILE="$RUN_DIR/tunnel.kind"
GUNICORN_LOG="$LOG_DIR/gunicorn.log"
GUNICORN_ACCESS_LOG="$LOG_DIR/gunicorn-access.log"
TUNNEL_LOG="$LOG_DIR/tunnel.log"

CHAT_PORT="${CHAT_PORT:-8025}"
CHAT_HOST="${CHAT_HOST:-127.0.0.1}"
CHAT_ACCESS_CODE="${CHAT_ACCESS_CODE:-}"
CHAT_DATA_DIR="${CHAT_DATA_DIR:-$SCRIPT_DIR/data}"
CHAT_VAPID_SUBJECT="${CHAT_VAPID_SUBJECT:-mailto:admin@localhost}"
export CHAT_PORT CHAT_HOST CHAT_ACCESS_CODE CHAT_DATA_DIR CHAT_VAPID_SUBJECT

# Default 'none': the production front door (romulator2k-ngrok.service ->
# nginx :8011 -> /chat/) is owned by systemd, so this launcher should not
# start a competing tunnel unless explicitly asked to.
TUNNEL="none"
FOREGROUND=0
NGROK_CONFIG=""
NGROK_AUTHTOKEN=""
COMMAND="start"

# ngrok local API used ONLY for configs we generate ourselves (never the
# default :4040/:4041, which belong to the other services on this box).
NGROK_WEB_ADDR="127.0.0.1:4055"

if [ -t 1 ]; then
    C_BOLD=$'\033[1m'; C_GREEN=$'\033[32m'; C_RED=$'\033[31m'; C_YEL=$'\033[33m'; C_OFF=$'\033[0m'
else
    C_BOLD=""; C_GREEN=""; C_RED=""; C_YEL=""; C_OFF=""
fi

info()  { printf '%s==>%s %s\n' "$C_BOLD" "$C_OFF" "$*"; }
ok()    { printf '%s[ok]%s %s\n' "$C_GREEN" "$C_OFF" "$*"; }
warn()  { printf '%s[warn]%s %s\n' "$C_YEL" "$C_OFF" "$*" >&2; }
die()   { printf '%s[err]%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }

usage() {
    sed -n '3,18p' "$SCRIPT_DIR/start.sh" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        start|stop|status) COMMAND="$1" ;;
        --tunnel=*)        TUNNEL="${1#*=}" ;;
        --tunnel)          shift; TUNNEL="${1:?--tunnel needs a value}" ;;
        --foreground|-f)   FOREGROUND=1 ;;
        --config)          shift; NGROK_CONFIG="${1:?--config needs a path}" ;;
        --authtoken)       shift; NGROK_AUTHTOKEN="${1:?--authtoken needs a token}" ;;
        -h|--help)         usage 0 ;;
        *)                 die "unknown argument: $1 (see --help)" ;;
    esac
    shift
done
case "$TUNNEL" in
    cloudflared|ngrok|none) ;;
    *) die "--tunnel must be cloudflared, ngrok or none (got: $TUNNEL)" ;;
esac

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
pid_file_pid() {  # echo PID if the pid file holds a live process, else nothing.
    # ALWAYS exits 0: callers use this in $() under set -e, and a dead pid
    # must not abort the script.
    local f="$1" pid=""
    [ -f "$f" ] || return 0
    pid="$(cat "$f" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        printf '%s' "$pid"
    fi
    return 0
}

cmdline_has() {  # cmdline_has <pid> <substring> - guards against PID reuse
    local pid="$1" needle="$2"
    [ -r "/proc/$pid/cmdline" ] || return 1
    tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q -- "$needle"
}

# The address we poll for health. 0.0.0.0 / :: are not always dialable.
HEALTH_HOST="$CHAT_HOST"
case "$HEALTH_HOST" in
    0.0.0.0|"") HEALTH_HOST="127.0.0.1" ;;
    "::"|"[::]") HEALTH_HOST="::1" ;;
esac
LOCAL_URL="http://$HEALTH_HOST:$CHAT_PORT/"

wait_for_health() {  # wait_for_health <seconds>
    local timeout="${1:-20}" i
    for ((i = 0; i < timeout * 2; i++)); do
        if curl -sf -m 2 "http://$HEALTH_HOST:$CHAT_PORT/api/health" >/dev/null 2>&1; then
            return 0
        fi
        sleep 0.5
    done
    return 1
}

db_path() { printf '%s/chat.db' "${CHAT_DATA_DIR%/}"; }

human_size() {  # human_size <path>
    if [ -e "$1" ]; then du -h "$1" | cut -f1; else echo "n/a (missing)"; fi
}

require_venv() {
    [ -x "$GUNICORN" ] || die "venv missing or has no gunicorn at $GUNICORN
Create it with:
  cd $SCRIPT_DIR
  python3 -m venv .venv
  ./.venv/bin/pip install -r requirements.txt"
}

# ---------------------------------------------------------------------------
# App (gunicorn) control
#
# ONE worker, 32 threads, --timeout 0.  The Flask app keeps SSE connections
# open forever and holds an in-memory SSE Bus + CLIENTS presence dict; a
# second worker would get its own copy, breaking fan-out and presence.
# --timeout 0 because gunicorn's default 30s worker timeout would kill idle
# SSE watchers.  --keep-alive 30 for the reverse-proxy/tunnel front ends.
# ---------------------------------------------------------------------------
GUNICORN_FLAGS=(
    --workers 1
    --worker-class gthread
    --threads 32
    --timeout 0
    --keep-alive 30
    --bind "$CHAT_HOST:$CHAT_PORT"
    --pid "$GUNICORN_PID_FILE"
    --error-logfile "$GUNICORN_LOG"
    --access-logfile "$GUNICORN_ACCESS_LOG"
    --capture-output
)

start_app() {
    require_venv
    local oldpid
    oldpid="$(pid_file_pid "$GUNICORN_PID_FILE")"
    if [ -n "$oldpid" ] && cmdline_has "$oldpid" gunicorn; then
        die "gunicorn already running (pid $oldpid). Run './start.sh stop' first."
    fi
    mkdir -p "$RUN_DIR" "$LOG_DIR" "$CHAT_DATA_DIR"

    if [ "$FOREGROUND" -eq 1 ]; then
        info "starting gunicorn in the FOREGROUND on $CHAT_HOST:$CHAT_PORT (Ctrl-C to stop)"
        "$GUNICORN" "${GUNICORN_FLAGS[@]}" app:app &
        APP_BG_PID=$!
        # give gunicorn a moment to write its pid file
        for _ in $(seq 1 20); do [ -s "$GUNICORN_PID_FILE" ] && break; sleep 0.25; done
    else
        info "starting gunicorn (daemon) on $CHAT_HOST:$CHAT_PORT"
        "$GUNICORN" "${GUNICORN_FLAGS[@]}" --daemon app:app
        local i
        for ((i = 0; i < 20; i++)); do
            [ -s "$GUNICORN_PID_FILE" ] && break
            sleep 0.25
        done
    fi

    if wait_for_health 20; then
        ok "app healthy: $(curl -sf -m 2 "http://$HEALTH_HOST:$CHAT_PORT/api/health")"
    else
        warn "app did not answer /api/health within 20s. Last log lines:"
        tail -n 20 "$GUNICORN_LOG" 2>/dev/null >&2 || true
        stop_app || true
        die "aborting (nothing left running)"
    fi
}

stop_app() {
    local pid
    pid="$(pid_file_pid "$GUNICORN_PID_FILE")"
    if [ -n "$pid" ] && cmdline_has "$pid" gunicorn; then
        info "stopping gunicorn (pid $pid)"
        kill -TERM "$pid" 2>/dev/null || true
        for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done
        if kill -0 "$pid" 2>/dev/null; then
            warn "gunicorn still alive after graceful wait, sending SIGKILL"
            kill -KILL "$pid" 2>/dev/null || true
        fi
        ok "gunicorn stopped"
    else
        info "gunicorn not running (no live pid in $GUNICORN_PID_FILE)"
    fi
    rm -f "$GUNICORN_PID_FILE"
}

# ---------------------------------------------------------------------------
# Tunnel control
# ---------------------------------------------------------------------------
start_tunnel() {
    mkdir -p "$RUN_DIR" "$LOG_DIR"
    : > "$TUNNEL_LOG"
    case "$TUNNEL" in
        cloudflared) start_cloudflared ;;
        ngrok)       start_ngrok ;;
        none)        start_none ;;
    esac
}

start_cloudflared() {
    command -v cloudflared >/dev/null 2>&1 ||
        die "cloudflared not found on PATH. Install it or use --tunnel=none."
    info "starting cloudflared quick tunnel -> http://127.0.0.1:$CHAT_PORT"
    cloudflared tunnel --url "http://127.0.0.1:$CHAT_PORT" --no-autoupdate \
        >>"$TUNNEL_LOG" 2>&1 &
    local pid=$!
    echo "$pid" > "$TUNNEL_PID_FILE"
    echo "cloudflared" > "$TUNNEL_KIND_FILE"

    local url="" i
    for ((i = 0; i < 60; i++)); do  # ~30s: quick tunnels can take a while
        url="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | head -n1 || true)"
        [ -n "$url" ] && break
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.5
    done
    if [ -z "$url" ]; then
        warn "no trycloudflare URL found in $TUNNEL_LOG. Last lines:"
        tail -n 20 "$TUNNEL_LOG" >&2 || true
        stop_tunnel || true
        die "cloudflared failed to start"
    fi
    echo "$url" > "$TUNNEL_URL_FILE"
    printf '\n%s*** PUBLIC URL: %s ***%s\n\n' "$C_BOLD$C_GREEN" "$url" "$C_OFF"
    warn "cloudflared quick tunnels get a NEW URL every restart; users must re-open the app (and re-subscribe for push) after each restart."
}

ngrok_explain() {  # ngrok_explain <headline>
    cat >&2 <<EOF

${C_RED}${C_BOLD}${1:-ngrok could not start.}${C_OFF}

Both ngrok accounts available on this box are ALREADY IN USE by other
protected services. Do NOT stop them:

  * Account A (default config ~/.config/ngrok/ngrok.yml) is held by the
    immutable, watchdog-protected unit orbit-ngrok@debian.service
    -> https://sulphuric-shortly-define.ngrok-free.dev -> 127.0.0.1:8099
  * Account B (/home/debian/projects/ROMulator2K/ngrok.yml) is held by
    romulator2k-ngrok.service
    -> https://uncolored-epidermis-saved.ngrok-free.dev -> 127.0.0.1:8011 (nginx)

Free ngrok allows only ONE online agent per account, so any additional
ngrok on those accounts fails while those services are up.

HOWEVER: this is already solved for you. The public URL above serves the
chat at /chat/ via nginx on 127.0.0.1:8011 (romulator2k-ngrok.service).
You do NOT need to start a tunnel at all - the default (none) is correct,
and the app itself runs as ngrok-chat.service.

Your options:
  1. (production) leave it alone: sudo systemctl status ngrok-chat.service
  2. An EXTRA, separate ephemeral tunnel (dev/testing only):
       ./start.sh --tunnel=cloudflared
  3. A FRESH ngrok authtoken (a third, unused account):
       ./start.sh --tunnel=ngrok --authtoken <your-token>

EOF
}

start_ngrok() {
    command -v ngrok >/dev/null 2>&1 ||
        die "ngrok not found on PATH. Install it or use --tunnel=cloudflared."

    local cfg="$NGROK_CONFIG"
    if [ -n "$NGROK_AUTHTOKEN" ]; then
        mkdir -p "$RUN_DIR"
        cfg="$(mktemp "$RUN_DIR/ngrok-chat-XXXXXX.yml")"
        chmod 600 "$cfg"
        cat > "$cfg" <<EOF
version: "3"
agent:
    authtoken: $NGROK_AUTHTOKEN
    web_addr: $NGROK_WEB_ADDR
tunnels:
    ngrok-chat:
        addr: 127.0.0.1:$CHAT_PORT
        proto: http
EOF
        info "wrote dedicated ngrok config $cfg (web_addr $NGROK_WEB_ADDR)"
    elif [ -n "$cfg" ]; then
        [ -f "$cfg" ] || die "ngrok config not found: $cfg"
    else
        # No --authtoken and no --config: the only fallback would be the
        # default config, whose account is held by orbit-ngrok. Don't even
        # try - ERR_NGROK_334 is guaranteed. Explain instead.
        ngrok_explain "Refusing to start ngrok: no --authtoken/--config was given, and the DEFAULT ngrok config (~/.config/ngrok/ngrok.yml) belongs to the already-occupied account A, so this would fail with ERR_NGROK_334."
        exit 1
    fi

    info "starting ngrok (named tunnel 'ngrok-chat', config $cfg) -> http://127.0.0.1:$CHAT_PORT"
    ngrok start --config "$cfg" ngrok-chat >>"$TUNNEL_LOG" 2>&1 &
    local pid=$!
    echo "$pid" > "$TUNNEL_PID_FILE"
    echo "ngrok" > "$TUNNEL_KIND_FILE"

    # Give the agent a moment, then look for the fatal account error first.
    sleep 3
    if grep -qE 'ERR_NGROK_334|already online' "$TUNNEL_LOG" 2>/dev/null; then
        stop_tunnel || true
        ngrok_explain "ngrok failed: ERR_NGROK_334 - \"domain already online\"."
        exit 1
    fi

    # Prefer the local API (only reliable for configs WE generated, which set
    # web_addr to :4055 - never the :4040/:4041 used by the other agents).
    local url="" i tunnels_json
    for ((i = 0; i < 40; i++)); do
        if tunnels_json="$(curl -sf -m 2 "$NGROK_WEB_ADDR/api/tunnels" 2>/dev/null)"; then
            url="$(printf '%s' "$tunnels_json" | grep -oE 'https://[a-z0-9-]+\.ngrok[a-z0-9.-]*(\.app|\.dev)' | head -n1 || true)"
        fi
        [ -z "$url" ] &&
            url="$(grep -oE 'https://[a-z0-9-]+\.ngrok[a-z0-9.-]*(\.app|\.dev)' "$TUNNEL_LOG" 2>/dev/null | head -n1 || true)"
        [ -n "$url" ] && break
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.5
    done

    if grep -qE 'ERR_NGROK_334|already online' "$TUNNEL_LOG" 2>/dev/null; then
        stop_tunnel || true
        ngrok_explain "ngrok failed: ERR_NGROK_334 - \"domain already online\"."
        exit 1
    fi
    if grep -q 'ERR_NGROK' "$TUNNEL_LOG" 2>/dev/null; then
        stop_tunnel || true
        warn "ngrok reported an error. Last log lines:"
        tail -n 20 "$TUNNEL_LOG" >&2 || true
        die "ngrok failed to start"
    fi
    if [ -z "$url" ]; then
        warn "ngrok started but no public URL detected. Last log lines:"
        tail -n 20 "$TUNNEL_LOG" >&2 || true
        die "could not determine ngrok URL"
    fi
    echo "$url" > "$TUNNEL_URL_FILE"
    printf '\n%s*** PUBLIC URL: %s ***%s\n\n' "$C_BOLD$C_GREEN" "$url" "$C_OFF"
}

start_none() {
    info "no tunnel requested (--tunnel=none)"
    rm -f "$TUNNEL_PID_FILE" "$TUNNEL_URL_FILE" "$TUNNEL_KIND_FILE"
}

stop_tunnel() {
    local pid
    pid="$(pid_file_pid "$TUNNEL_PID_FILE")"
    if [ -n "$pid" ]; then
        if cmdline_has "$pid" cloudflared || cmdline_has "$pid" ngrok; then
            info "stopping tunnel (pid $pid)"
            kill -TERM "$pid" 2>/dev/null || true
            for _ in $(seq 1 10); do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done
            kill -KILL "$pid" 2>/dev/null || true
            ok "tunnel stopped"
        else
            warn "pid $pid in $TUNNEL_PID_FILE is not our tunnel (PID reuse?); not killing"
        fi
    fi
    rm -f "$TUNNEL_PID_FILE" "$TUNNEL_URL_FILE" "$TUNNEL_KIND_FILE"
}

print_summary() {
    local public="no tunnel"
    if [ -s "$TUNNEL_URL_FILE" ]; then
        public="$(cat "$TUNNEL_URL_FILE")"
    fi
    cat <<EOF

$C_BOLD-------------------------------------------------------------$C_OFF
$C_BOLD ngrok-chat summary$C_OFF
  local URL : $LOCAL_URL
  public URL: $public
  app log   : $GUNICORN_LOG
  app log   : $GUNICORN_ACCESS_LOG
  pid files : $GUNICORN_PID_FILE $TUNNEL_PID_FILE
  tunnel log: $TUNNEL_LOG
  database  : $(db_path) ($(human_size "$(db_path)"))
$C_BOLD-------------------------------------------------------------$C_OFF
EOF
}

# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------
do_start() {
    # Preflight: don't boot the app just to fail on a doomed ngrok setup.
    if [ "$TUNNEL" = "ngrok" ] && [ -z "$NGROK_AUTHTOKEN" ] && [ -z "$NGROK_CONFIG" ]; then
        ngrok_explain "Refusing to start: --tunnel=ngrok needs --authtoken <fresh-token> or --config <path> on this box."
        exit 1
    fi

    if [ "$FOREGROUND" -eq 1 ]; then
        # Foreground: run gunicorn attached to this terminal; tunnel (if any)
        # runs in the background and is cleaned up on exit.
        start_app
        if [ "$TUNNEL" != "none" ]; then
            start_tunnel
            trap 'info "shutting down"; stop_tunnel || true; kill "$APP_BG_PID" 2>/dev/null || true' EXIT INT TERM
        fi
        print_summary
        wait "$APP_BG_PID"
        exit $?
    fi

    start_app
    if [ "$TUNNEL" != "none" ]; then
        start_tunnel
    else
        start_none
        info "app is local-only: $LOCAL_URL (no public tunnel)"
    fi
    print_summary
    info "manage with: $SCRIPT_DIR/start.sh status | stop"
}

do_stop() {
    stop_tunnel
    stop_app
    ok "stopped"
}

do_status() {
    local app_pid tunnel_pid healthy="no" health_json=""
    app_pid="$(pid_file_pid "$GUNICORN_PID_FILE")"
    [ -n "$app_pid" ] && cmdline_has "$app_pid" gunicorn || app_pid=""
    tunnel_pid="$(pid_file_pid "$TUNNEL_PID_FILE")"

    echo "app    : $( [ -n "$app_pid" ] && echo "running (pid $app_pid)" || echo "not running" )"
    if health_json="$(curl -sf -m 3 "http://$HEALTH_HOST:$CHAT_PORT/api/health" 2>/dev/null)"; then
        healthy="yes"
        echo "health : $health_json"
    else
        echo "health : no answer at http://$HEALTH_HOST:$CHAT_PORT/api/health"
    fi
    if [ -n "$tunnel_pid" ]; then
        echo "tunnel : running (pid $tunnel_pid, $(cat "$TUNNEL_KIND_FILE" 2>/dev/null || echo '?'))"
    else
        echo "tunnel : not running"
    fi
    if [ -s "$TUNNEL_URL_FILE" ]; then
        echo "public : $(cat "$TUNNEL_URL_FILE")"
    else
        echo "public : none"
    fi
    echo "logs   : $GUNICORN_LOG , $TUNNEL_LOG"
    echo "db     : $(db_path) ($(human_size "$(db_path)"))"
    [ "$healthy" = "yes" ]
}

case "$COMMAND" in
    start)  do_start ;;
    stop)   do_stop ;;
    status) do_status ;;
esac
