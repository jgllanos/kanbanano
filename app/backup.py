"""Write a consistent copy of the database to stdout.

    python -m app.backup > board-backup.db

Meant to be run inside the container (backup.sh does that), since the database
lives in a Docker volume the host can't easily read. Safe while the app is
running: VACUUM INTO takes a snapshot, so writes landing mid-backup don't end
up half in the copy. The copy is a single self-contained file, and compacted.
"""

import shutil
import sys
import tempfile
from pathlib import Path

from app import db


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "backup.db"
        conn = db.connect()
        try:
            conn.execute("VACUUM INTO ?", (str(copy),))
        finally:
            conn.close()
        with copy.open("rb") as f:
            shutil.copyfileobj(f, sys.stdout.buffer)


if __name__ == "__main__":
    main()
