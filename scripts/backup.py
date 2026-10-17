"""Safe SQLite Online Backup API snapshot; call with source/output filenames."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys


def backup(source: Path, destination: Path) -> None:
    if not source.exists():
        raise FileNotFoundError(f"SQLite database not found: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite backup: {destination}")
    # sqlite3.Connection as a context manager commits/rolls back but does NOT close!
    with closing(sqlite3.connect(f"file:{source.absolute()}?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(destination)) as target:
            original.backup(target)
            result = target.execute("PRAGMA integrity_check").fetchone()
            if result is None or result[0] != "ok":
                raise RuntimeError(f"SQLite integrity check failed: {result}")
    print(f"Backup saved: {destination}")


if __name__ == "__main__":
    source = Path(os.environ.get("WB_DB_PATH", "data/serp.db"))
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else source.parent / (
        "backup-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ") + ".sqlite")
    backup(source, output)
