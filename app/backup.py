"""Write a consistent copy of the database to stdout.

    python -m app.backup > board-backup.db

backup.sh runs this inside the container, because the database is in a Docker
volume. It's safe while the app is running: VACUUM INTO copies a consistent
snapshot into a single, compacted file.
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
