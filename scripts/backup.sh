#!/usr/bin/env bash
#
# backup.sh - consistent SQLite backup of ngrok-chat's chat.db.
#
# The DB runs in WAL mode, so `cp data/chat.db backups/...` can miss data
# still in the -wal file or copy a torn page set. The sqlite3 CLI is NOT
# installed on this box, so we use Python's sqlite3 online backup API through
# the project venv - that is atomic wrt concurrent writers and safe to run
# from cron.
#
#   ./scripts/backup.sh              # keep newest 14 backups
#   ./scripts/backup.sh --keep 30    # keep newest 30
#
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

VENV_PY="$PROJECT_DIR/.venv/bin/python"
DATA_DIR="${CHAT_DATA_DIR:-$PROJECT_DIR/data}"
DB="$DATA_DIR/chat.db"
BACKUP_DIR="$PROJECT_DIR/backups"
KEEP=14

while [ $# -gt 0 ]; do
    case "$1" in
        --keep) shift; KEEP="${1:?--keep needs a number}"; ;;
        --keep=*) KEEP="${1#*=}" ;;
        -h|--help) sed -n '3,14p' "$SCRIPT_DIR/backup.sh" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1 (see --help)" >&2; exit 1 ;;
    esac
    shift
done
case "$KEEP" in
    ''|*[!0-9]*) echo "--keep must be a positive integer (got: $KEEP)" >&2; exit 1 ;;
esac
[ "$KEEP" -ge 1 ] || { echo "--keep must be >= 1" >&2; exit 1; }

[ -f "$DB" ] || { echo "no database at $DB - nothing to back up" >&2; exit 1; }
[ -x "$VENV_PY" ] || { echo "venv python missing: $VENV_PY" >&2; exit 1; }

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$BACKUP_DIR/chat-$STAMP.db"

# sqlite3.connect(src).backup(dst) -> consistent snapshot even under WAL and
# even while gunicorn is serving traffic. (Open read-write: a WAL-mode DB
# opened mode=ro may need the -wal/-shm files, and backup() is designed to
# run against a live DB anyway.)
"$VENV_PY" - "$DB" "$DEST" <<'PY'
import sqlite3
import sys

src_path, dst_path = sys.argv[1], sys.argv[2]
src = sqlite3.connect(src_path)
dst = sqlite3.connect(dst_path)
try:
    with dst:
        src.backup(dst)
    try:
        n = dst.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    except sqlite3.Error:
        n = "unknown (no messages table yet)"
finally:
    dst.close()
    src.close()
print(f"snapshot ok: messages={n}")
PY

# Sanity: non-empty file, and the snapshot must be at least page-sized.
SIZE_BYTES="$(stat -c %s "$DEST")"
if [ "$SIZE_BYTES" -lt 512 ]; then
    echo "backup looks empty ($SIZE_BYTES bytes), removing: $DEST" >&2
    rm -f "$DEST"
    exit 1
fi

echo "wrote $DEST ($(du -h "$DEST" | cut -f1), $SIZE_BYTES bytes)"

# Rotate: delete everything except the KEEP newest chat-*.db snapshots.
# find -printf %T@ + sort gives a stable mtime ordering; older files are
# matched with -delete via a piped xargs (null-safe).
DELETED=0
while IFS= read -r old; do
    [ -n "$old" ] || continue
    rm -f -- "$old"
    DELETED=$((DELETED + 1))
done < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'chat-*.db' -printf '%T@ %p\n' \
            | sort -rn | tail -n +$((KEEP + 1)) | cut -d' ' -f2-)

TOTAL="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'chat-*.db' | wc -l)"
echo "rotation: kept newest $KEEP, deleted $DELETED, $TOTAL snapshot(s) now in $BACKUP_DIR"
