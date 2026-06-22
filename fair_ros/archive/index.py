"""SQLite mission index (specs/archive.md).

The index is a query cache for `ros2 fair list`; the archive directories'
mission_record.json files are the source of truth, and reindex() can rebuild
the database from them at any time.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

from fair_ros.manifest.schema import MissionRecord
from fair_ros.utils import paths

DB_VERSION = "1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS missions (
    mission_id        TEXT PRIMARY KEY,
    created_at        TEXT NOT NULL,
    operator          TEXT NOT NULL,
    location          TEXT NOT NULL,
    goal              TEXT NOT NULL,
    archive_path      TEXT NOT NULL UNIQUE,
    duration_s        REAL NOT NULL DEFAULT 0,
    size_bytes        INTEGER NOT NULL DEFAULT 0,
    bag_count         INTEGER NOT NULL DEFAULT 0,
    warning_count     INTEGER NOT NULL DEFAULT 0,
    robot_name        TEXT,
    fair_ros_version  TEXT,
    schema_version    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_missions_created
    ON missions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_missions_operator
    ON missions(operator COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_missions_location
    ON missions(location COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(paths.index_db_path(), timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA busy_timeout = 5000")
    con.executescript(_SCHEMA)
    con.execute("INSERT OR IGNORE INTO meta VALUES ('db_version', ?)",
                (DB_VERSION,))
    return con


def _row_from_record(record: MissionRecord, archive_path: Path) -> tuple:
    return (
        record.identity.mission_id,
        record.identity.created_at.isoformat(),
        record.identity.operator_name,
        record.intent.location_name,
        record.intent.goal,
        str(archive_path),
        sum(b.duration_s or 0 for b in record.bags),
        sum(b.size_bytes for b in record.bags),
        len(record.bags),
        sum(len(b.health_warnings) for b in record.bags),
        record.robot.name if record.robot else None,
        record.provenance.fair_ros_version,
        record.schema_version,
    )


def insert(record: MissionRecord, archive_path: Path) -> None:
    with _connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO missions VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _row_from_record(record, archive_path))


def query(operator: str | None = None, location: str | None = None,
          since: str | None = None, until: str | None = None,
          limit: int = 20) -> tuple[list[dict[str, Any]], int]:
    """Filtered mission rows, newest first. Returns (rows, total_matching)."""
    where, params = [], []
    if operator:
        where.append("operator LIKE ? COLLATE NOCASE")
        params.append(f"%{operator}%")
    if location:
        where.append("location LIKE ? COLLATE NOCASE")
        params.append(f"%{location}%")
    if since:
        where.append("created_at >= ?")
        params.append(since)
    if until:
        where.append("created_at < ?")
        # until is an inclusive date; bump to the end of that day
        params.append(until + "T23:59:59.999999+00:00"
                      if len(until) == 10 else until)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with _connect() as con:
        total = con.execute(
            f"SELECT COUNT(*) FROM missions{clause}", params).fetchone()[0]
        rows = con.execute(
            f"SELECT * FROM missions{clause} ORDER BY created_at DESC "
            f"LIMIT ?", [*params, limit]).fetchall()
    return [dict(r) for r in rows], total


def reindex(archive_root: Path | None = None) -> int:
    """Rebuild the index by scanning archive dirs for mission_record.json."""
    archive_root = archive_root or paths.archive_dir()
    count = 0
    with _connect() as con:
        con.execute("DELETE FROM missions")
        for record_file in sorted(archive_root.glob("*/mission_record.json")):
            try:
                record = MissionRecord.model_validate(
                    json.loads(record_file.read_text()))
            except Exception:
                continue
            con.execute(
                "INSERT OR REPLACE INTO missions VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _row_from_record(record, record_file.parent))
            count += 1
    return count
