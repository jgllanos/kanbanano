#!/bin/sh
# Copy the running app's database to DIR/board-YYYY-MM-DD.db.
#
#   ./backup.sh /path/to/backups
#
# Run from anywhere; it finds compose.yaml next to itself.
set -eu

dest="${1:?usage: backup.sh DIR}"
name="board-$(date +%F).db"

cd "$(dirname "$0")"
mkdir -p "$dest"

# Write to a temporary name first, so a failed run never leaves a truncated file
# that looks like a good backup.
docker compose exec -T kanbanano python -m app.backup > "$dest/.$name.partial"
mv "$dest/.$name.partial" "$dest/$name"
echo "Backed up to $dest/$name"
