#!/usr/bin/env bash
#
# Restore the metadata catalog from a dump.
#
# Run this QUARTERLY against a scratch database, and write the date in
# docs/runbook.md. An untested backup is not a backup — it is a hope.
#
#   ./scripts/restore_metadata.sh                       # restore-test into lake_meta_restoretest
#   ./scripts/restore_metadata.sh --target lake_meta    # the real thing. asks first.
#
set -euo pipefail

DEST="${LAKE_NAS_ROOT:-/mnt/nas/lake}/_meta/backups"
TARGET="lake_meta_restoretest"
DUMP=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target) TARGET="$2"; shift 2 ;;
        --dump)   DUMP="$2";   shift 2 ;;
        *) echo "unknown flag: $1" >&2; exit 2 ;;
    esac
done

DUMP="${DUMP:-$(ls -t "$DEST"/lake_meta_*.dump 2>/dev/null | head -1)}"
[[ -n "$DUMP" && -f "$DUMP" ]] || { echo "no dump found in $DEST" >&2; exit 1; }

echo "dump:   $DUMP"
echo "target: $TARGET"

if [[ -f "$DUMP.sha256" ]]; then
    echo "verifying checksum..."
    (cd "$(dirname "$DUMP")" && sha256sum -c "$(basename "$DUMP").sha256")
fi

# Restoring over the live catalog destroys the run history. Make it deliberate.
if [[ "$TARGET" != *restoretest* ]]; then
    printf '\033[31mThis will DROP and recreate the database "%s".\033[0m\n' "$TARGET"
    read -rp 'Type the database name to confirm: ' confirm
    [[ "$confirm" == "$TARGET" ]] || { echo "aborted"; exit 1; }
fi

sudo -u postgres dropdb --if-exists "$TARGET"
sudo -u postgres createdb "$TARGET" -O lake
sudo -u postgres pg_restore -d "$TARGET" --no-owner --role=lake "$DUMP"

echo
echo "restored. sanity check:"
sudo -u postgres psql -d "$TARGET" -c '
    SELECT (SELECT count(*) FROM sources) AS sources,
           (SELECT count(*) FROM runs)    AS runs,
           (SELECT count(*) FROM files)   AS files,
           (SELECT max(started_at) FROM runs) AS newest_run;'

echo
echo "If those numbers look right, the backup is good."
echo "Record today's date in docs/runbook.md under 'Last restore test'."
