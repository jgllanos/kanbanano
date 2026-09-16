"""The backup module, run the way backup.sh runs it."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent


def test_backup_writes_a_usable_copy_to_stdout(client, conn, board, tmp_path):
    # A write through the app, so the copy has to include what's in the WAL.
    client.post("/cards", data={"list_id": board.todo, "title": "Paint"})
    (db_path,) = conn.execute("SELECT file FROM pragma_database_list WHERE name = 'main'").fetchone()

    result = subprocess.run(
        [sys.executable, "-m", "app.backup"],
        cwd=REPO,
        env={**os.environ, "DB_PATH": db_path},
        capture_output=True,
        check=True,
    )
    copy = tmp_path / "copy.db"
    copy.write_bytes(result.stdout)

    restored = sqlite3.connect(copy)
    titles = {title for (title,) in restored.execute("SELECT title FROM cards")}
    assert titles == {"Tap", "Bins", "Shelf", "Paint"}
    assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
