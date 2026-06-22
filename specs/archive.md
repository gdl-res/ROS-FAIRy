# Spec: Archive & Mission Index

`archive/assembler.py` turns a validated spool into a self-contained RO-Crate
directory under `/var/fair-ros/archive/` and registers it in the SQLite index.
It runs only from `mission_close`, after the operator confirms "Save".

## Directory naming

```
/var/fair-ros/archive/<YYYY-MM-DD>_<location>_<operator>/
```

- Date from `identity.created_at` (local time).
- `<location>` and `<operator>` are sanitised: lowercase, ASCII-transliterated,
  non-alphanumerics collapsed to single `-`, trimmed, max 40 chars each.
  ("Marsh Creek, north bank" → `marsh-creek-north-bank`).
- Collision (same day, place, operator): append `_2`, `_3`, …

## Crate layout

```
2026-06-12_marsh-creek-north-bank_jane-doe/
├── ro-crate-metadata.json        # JSON-LD, see specs/ro_crate_schema.md
├── mission_record.json           # the full MissionRecord, machine-readable
├── README.md                     # generated plain-language summary (the
│                                 #   mission_close panel content as markdown)
├── bags/
│   └── rosbag2_2026_06_12-14_02_58/   # moved verbatim, incl. metadata.yaml
├── harvest/
│   ├── harvest.json              # raw harvest as the watchdog left it
│   ├── robot_description.urdf   # only if captured
│   ├── tf_static.json            # only if captured
│   ├── pip_freeze.txt            # only if pip freeze succeeded
│   ├── lsusb_verbose.txt         # only if lsusb -v was permitted
│   └── dmesg_usb.txt             # only if dmesg was permitted
├── calibrations/
│   └── camera_front_intrinsics.yaml   # copied, sha256 recorded in manifest
└── docker/
    ├── containers.json           # docker inspect output, verbatim
    └── compose/<project>/docker-compose.yml   # snapshot per compose project
```

Sections with nothing to hold (no docker, no calibrations) are omitted entirely
— no empty directories.

## Bags: move, don't copy

Spool and archive live on the same filesystem (`/var/fair-ros/...` — `setup`
must keep it that way), so bag directories are moved with `os.rename`: instant
and atomic regardless of size. Mission data on field robots can be tens of GB;
copying would double peak disk usage and take minutes.

Fallback: if `rename` raises `EXDEV` (someone mounted archive elsewhere), fall
back to copy-then-delete per bag: `shutil.copytree` → verify total size matches
→ delete spool original. The progress bar in `mission_close` covers this path.

Before the move, the assembler hashes every file in each bag and records the
digests in `bags[].file_sha256` (bag-relative path → sha256). Hashing the spool
copy is safe — `rename` and the EXDEV copy-fallback both preserve bytes — and
lets `ros2 fair verify` detect byte-level bag modification later.

Small files (calibrations, compose files, harvest artifacts) are **copied**, not
moved — their originals belong to the robot's live configuration. Each copy gets
a sha256 recorded in the manifest (`calibrations[].sha256`).

## Raw harvest artifacts

Mirroring the Docker pattern, bulky raw text captured at harvest time lives in
`harvest.json` (not the manifest) and is extracted to plain files by the
assembler. Each is written only when the corresponding source produced data;
absent sources leave no file (no empty placeholders):

- `harvest/pip_freeze.txt` — from `raw_python_env.pip_freeze`.
- `harvest/lsusb_verbose.txt` — from `raw_hardware.lsusb_verbose`.
- `harvest/dmesg_usb.txt` — from `raw_hardware.dmesg_usb` (already filtered to
  USB/video/tty/camera/serial/sensor lines by the harvester).

The structured `PythonEnv` and `hardware_devices[]` summaries in
`mission_record.json` never contain these raw blobs.

## Docker snapshotting

Done at **harvest time** by `docker_info.py` (data collection) and at **assembly
time** by the assembler (file copies):

- `containers.json`: raw `docker inspect` array of all running containers.
- Image digests: `RepoDigests[0]` per container; `null` for never-pushed local
  builds (recorded as such — a warning is generated: "Some software containers
  couldn't be pinned to an exact version").
- Compose discovery: label `com.docker.compose.project.config_files` on any
  container; each distinct file that still exists is copied to
  `docker/compose/<project>/`. Missing files are noted in the manifest, not fatal.

## Assembly algorithm (failure-safe)

```
1. staging = /var/fair-ros/archive/.staging/<final-name>/   (wiped if pre-existing)
2. Copy small artifacts into staging (harvest/, calibrations/, docker/, README.md)
3. Write mission_record.json + ro-crate-metadata.json into staging
   (paths inside them are crate-relative and already final)
4. MOVE bag directories into staging/bags/        ← the only step touching spool data
5. fsync + os.rename(staging → final archive dir)  ← commit point
6. INSERT mission row into SQLite index
7. Delete leftover spool files (mission_context.json, harvest.json)
```

Failure handling:

| Failure point | Result |
|---|---|
| Steps 1–3 fail | Delete staging. Spool untouched. Plain error, exit 1. |
| Step 4 fails midway (e.g. disk full during EXDEV copy) | Move already-moved bags **back** to spool, delete staging. Spool restored. Error names the cause. |
| Step 5 fails | Bags are in staging, not spool. Do **not** delete staging — print: "Saving was interrupted; your data is safe in <staging path>." Next `mission_close` run detects a staging dir and offers to resume the commit. |
| Step 6 fails (index locked/corrupt) | Archive dir is complete and kept. Print warning: archived fine but won't appear in `ros2 fair list`; `index.py` provides a `reindex()` that rebuilds the DB by scanning archive dirs for `mission_record.json`. |
| Step 7 fails | Harmless; stale spool JSONs are overwritten by the next mission_start. |

Invariant: **at every instant, each bag exists in exactly one place, and the
final archive directory either doesn't exist or is complete.** The SQLite index
is a cache, never the source of truth.

## SQLite index

`/var/fair-ros/index.db`, accessed only via `archive/index.py`
(`sqlite3` stdlib, WAL mode, `busy_timeout=5000`).

```sql
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS missions (
    mission_id        TEXT PRIMARY KEY,
    created_at        TEXT NOT NULL,      -- ISO 8601 UTC
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

CREATE INDEX IF NOT EXISTS idx_missions_created  ON missions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_missions_operator ON missions(operator COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_missions_location ON missions(location COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS meta (        -- schema migrations
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);  -- row ('db_version', '1')
```

`index.py` API: `insert(record, archive_path)`, `query(filters) -> rows`,
`reindex(archive_root)`. All values come from the `MissionRecord`; the index
never stores anything not recoverable from `mission_record.json` files.
