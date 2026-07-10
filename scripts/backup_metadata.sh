#!/usr/bin/env bash
#
# Nightly metadata backup. Run from lake-backup.timer, or by hand.
#
# The metadata catalog is the crown jewel. Raw files are often re-downloadable;
# the run history, the checksums, and the lineage are not. If you back up one
# thing on this machine, back up this.
#
set -euo pipefail

DB="${LAKE_DB:-lake_meta}"
DEST="${LAKE_NAS_ROOT:-/mnt/nas/lake}/_meta/backups"
KEEP_DAYS="${KEEP_DAYS:-30}"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

command -v pg_dump >/dev/null || { echo "pg_dump not found" >&2; exit 1; }
[[ -f "${LAKE_NAS_ROOT:-/mnt/nas/lake}/.lake_mounted" ]] || {
    echo "NAS not mounted — refusing to write backup" >&2; exit 1; }

mkdir -p "$DEST"
OUT="$DEST/${DB}_${STAMP}.dump"

# -Fc is the custom format: compressed, and pg_restore can read it selectively.
pg_dump -Fc -d "$DB" -f "$OUT"
sha256sum "$OUT" > "$OUT.sha256"
chmod 0440 "$OUT" "$OUT.sha256"

echo "wrote $OUT ($(du -h "$OUT" | cut -f1))"

# Prune, but only after the new dump landed successfully.
find "$DEST" -name "${DB}_*.dump*" -mtime "+$KEEP_DAYS" -delete
echo "pruned dumps older than ${KEEP_DAYS}d"

# Offsite. restic dedupes, so nightly dumps of a mostly-static DB cost little.
if [[ -n "${RESTIC_REPOSITORY:-}" ]]; then
    restic backup "$DEST" "${LAKE_NAS_ROOT:-/mnt/nas/lake}/processed"
    restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 12 --prune
    echo "pushed offsite"
fi
