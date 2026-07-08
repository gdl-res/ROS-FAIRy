# Spec: Watchdog Service

`fair_ros/watchdog/watchdog.py`, run by systemd unit `fair-ros-watchdog.service`
as a simple long-lived process (`Type=simple`, `Restart=always`, `RestartSec=5`).
The watchdog is the "dashcam": it notices recordings starting and stopping and
harvests context with zero operator involvement.

## inotify watch targets

rosbag2 records into a **directory** (`<bag>/<bag>_0.db3` or `..._0.mcap` + `metadata.yaml`),
and inotify is not recursive, so two watch levels are used:

| Watch | Path | Events | Purpose |
|---|---|---|---|
| W1 (permanent) | `/var/fair-ros/spool/bags/` | `IN_CREATE \| IN_ISDIR`, `IN_MOVED_TO` | Detect a new bag directory. |
| W2 (per active bag) | `/var/fair-ros/spool/bags/<bagdir>/` | `IN_CREATE`, `IN_MODIFY`, `IN_CLOSE_WRITE` | Detect storage chunks (`.db3` **or** `.mcap`) being written and `metadata.yaml` being closed. |

Throughout this spec, "storage file" means a file matching `*.db3` or `*.mcap`
(Jazzy's default rosbag2 storage is MCAP; sqlite3 remains selectable).

W2 is added when W1 fires and removed when the bag is finalised. If
`/var/fair-ros/spool/bags/` does not exist at startup, the watchdog creates it
(mode `0775`).

**Arm-time scan (inotify race).** inotify only delivers events that occur
*after* `add_watch`. A bag whose first storage file is created in the window
between the directory appearing (W1) and W2 being armed — or a finished bag
directory moved into the spool with its chunks already present — would never
produce a storage-file CREATE on W2 and so would be missed (no RECORDING, no
context harvest). Therefore, immediately after arming W2 on a new directory,
the watchdog scans it once for an existing storage file and applies the same
transition the live CREATE would have (IDLE → RECORDING, or queue while
RECORDING). This makes detection robust to the race during normal operation,
mirroring the startup recovery scan below.

Library: `inotify_simple` (single dependency, no callback framework — we run our
own select loop so we can also service timers).

## State machine

```
            new bag dir + first storage file created
   IDLE ────────────────────────────────────────▶ RECORDING
    ▲                                                 │
    │                                                 │ metadata.yaml CLOSE_WRITE
    │      bag entry appended to harvest.json,        │   OR ≥ 30 s with no write
    │      state file updated                         ▼
    └──────────────────────────────────────────  FINALISING
```

Exactly one bag directory is active at a time (single-robot, single-mission
assumption). If a second bag directory appears while RECORDING, it is queued and
processed after the current one finalises; the overlap is logged. This is a hard
invariant: a foreign recording found by the recorder-process poller (see
*Foreign-bag detection* below) is subject to the **same** queue — it is never
harvested concurrently with the active bag, regardless of which one is the
spool bag.

### IDLE → RECORDING

Trigger, whichever comes first:
- first storage file (`.db3`/`.mcap`) created inside a new bag directory under W1
  (`metadata.yaml` alone does not trigger — rosbag2 writes it only on close), or
- the recorder-process poller detects a **new foreign recording** (a
  `ros2 bag record` started outside the spool — see *Foreign-bag detection*).

On entry, before the pipeline, the watchdog adopts the **live recording shell's**
DDS discovery settings if `<spool>/session.env` exists (written by
`mission_start` / `mission_record`), overlaying its own environment. The
watchdog's own env is the frozen `watchdog.env` snapshot from setup, which goes
blind under domain / RMW drift; adopting the recorder's discovery keys keeps the
harvest's `ros2` subprocesses and rclpy on the same DDS partition as the session
being recorded. Only `ros_env.SESSION_ADOPT_KEYS` (domain, RMW, discovery range
/ peers) are adopted — `session.env` is group-writable and the watchdog runs as
root, so loader paths (`PATH` / `LD_LIBRARY_PATH` / `PYTHONPATH` / overlay) are
never trusted from it; the base ROS install comes only from `watchdog.env`.

For a **foreign** recording (no `mission_record`, so no `session.env`), the same
discovery keys are read instead from the recorder process's own
`/proc/<pid>/environ` — the recorder is, by definition, on the partition we need
to harvest, so its environment is the authoritative source. The identical
security rule applies: only `SESSION_ADOPT_KEYS` are adopted from `/proc/environ`,
never loader paths.

Then run the harvest pipeline **in this order** (cheap and local first, so a
broken ROS graph cannot delay capturing what is capturable):

1. `harvest/robot_identity.py` — read + validate the yaml. No timeout needed.
2. `harvest/system_info.py` — hostname, kernel, arch, `$ROS_DISTRO`, dpkg versions.
3. `harvest/python_env.py` — interpreter + installed packages via
   `importlib.metadata` (no subprocess), then best-effort `pip freeze` /
   `pip list --format=json`, **30 s total**. pip failure → status `partial`,
   structured packages still captured.
4. `harvest/hardware_devices.py` — `lsusb`, `lspci`, `/dev/*` globs, `udevadm`,
   `v4l2-ctl`, filtered `dmesg`. **10 s per command**, **60 s total**
   (`lsusb -v` gets 20 s). Missing binaries / permission denials → status
   `partial` (or `skipped` if nothing is available at all). Read-only.
5. `harvest/ros_graph.py` — subprocess calls, **20 s timeout each**:
   `ros2 node list`, `ros2 topic list -t`, `ros2 pkg list`, then
   `ros2 param dump <node>` per node (param dumps capped at 60 s total; nodes
   not dumped in time are skipped, `ros_graph.complete = false`).
6. `harvest/docker_info.py` — `docker ps -q` + `docker inspect`, **10 s timeout
   total**. Absent/timeouted Docker → empty list, status `skipped`/`timeout`.
7. `harvest/ros_descriptions.py` — minimal rclpy node, **5 s timeout** waiting
   for `/robot_description` and `/tf_static` (transient-local subscriptions).
   Timeout → both `None`, status `timeout`.

Steps 3–4 are ROS-independent and run before the ROS graph so a broken or
not-yet-started ROS environment cannot delay capturing them. The canonical
module list (and `harvest_status` keys) is `manifest/builder.HARVEST_MODULES`.

Sensor liveness (`sensors[].detected_at_start`) is computed from steps 1 + 5.

Result is written **atomically** (write `harvest.json.tmp`, `fsync`, `rename`) to
`/var/fair-ros/spool/harvest.json`, with `provenance.harvest_status` recording
per-module outcome. Harvest never raises out of the pipeline: each module is
wrapped, failures are logged to the journal and recorded in `harvest_status`.

**ROS retry rule:** if `ros_graph` or `ros_descriptions` failed (ROS 2 not up yet
— common right after boot), the watchdog retries those two modules every **60 s**
while still in RECORDING, rewriting `harvest.json` on first success. Other
modules — including `python_env` and `hardware_devices` — are not retried; they
run once and their `partial`/`failed` status is recorded but not chased.

### RECORDING → FINALISING

Trigger, whichever comes first:

- `IN_CLOSE_WRITE` on `metadata.yaml` in the active bag dir (clean rosbag2 stop), or
- **30 s** (bag inactivity threshold) with no `IN_MODIFY`/`IN_CLOSE_WRITE` on any
  storage file in the active dir (crash / power-pull case).

### FINALISING → IDLE

1. Parse the bag's `metadata.yaml` (if missing — hard crash mid-write — recover
   what is possible: file sizes, mtimes; mark the bag entry with a
   `never_published`-style warning "Recording ended unexpectedly").
2. Run `utils/topic_health.py` over the bag metadata → `health_warnings`.
   Metadata-level checks (`never_published`) run for any storage format.
   Timestamp-level checks (`gap`, `low_rate`) need per-message receive times,
   which are read through `utils/bag_storage.py`'s pluggable reader registry:
   `sqlite3` (stdlib) and `mcap` (Jazzy's default, via the optional `mcap`
   package) are both implemented. If `mcap` is not installed those bags fall
   back to metadata-level checks. Callers never branch on storage format —
   they consult `bag_storage.get_reader(storage_id)`.
3. Append the `Bag` record to `harvest.json.bags[]` (atomic rewrite).
4. Set `provenance.harvested_at`, remove W2, update `watchdog.state`, go IDLE.

No archiving happens here. Archiving is exclusively triggered by the operator via
`ros2 fair mission_close`.

## Foreign-bag detection (recordings started outside `mission_record`)

The dashcam must FAIR-ify *any* recording, not only those started via
`ros2 fair mission_record`. The wrapper records into the spool, where inotify
sees it; a plain `ros2 bag record -o ~/run42 /scan` in another terminal lands in
the operator's cwd and is otherwise invisible. To close this gap the watchdog
runs a **recorder-process poller** alongside the inotify/select loop.

**Detection — `/proc` scan (DECIDED, PR #32).** Every `FOREIGN_SCAN_INTERVAL_S`
the watchdog scans `/proc` for a live rosbag2 **recorder** process:

- Match the rosbag2 recorder by its command line (`ros2 bag record …` / the
  rosbag2 recorder executable). **Exclude** `play` / `info` / `convert` /
  `reindex` — only an active *recorder* counts. Process detection (not
  filesystem watching) is what distinguishes a real recording from someone
  copying a `.mcap` file around.
- Resolve the output directory **from the recorder's open file descriptors
  first** (`/proc/<pid>/fd/*` — rosbag2 holds its `.db3`/`.mcap` open for the
  whole recording, so this is authoritative and resolves the instant recording
  starts, with no argv or cwd guessing). Fall back to the recorder's
  `--output`/`-o` argument, resolving a relative path against
  `/proc/<pid>/cwd`; with no `-o`, rosbag2's default is
  `rosbag2_<timestamp>/` in the cwd.
- A recorder whose cmdline matches but whose output cannot be resolved is
  logged (WARNING, once per pid) so the journal explains any miss.
- **Dedupe.** Ignore any output dir that is already tracked: spool bags from
  `mission_record` (already covered by inotify), foreign bags already being
  tracked, and anything already present in `harvest.json.bags[]`.

This is robot- and version-agnostic (just `/proc`), catches CLI *and*
launch-file recordings, and lets harvest fire at true record-start.

**On a newly detected foreign recording (when IDLE):**

1. Adopt DDS discovery keys from the recorder's `/proc/<pid>/environ`
   (`ros_env.SESSION_ADOPT_KEYS` only — see *IDLE → RECORDING* above for the
   security rule), so the harvest lands on the recorder's partition without a
   `session.env`.
2. Arm an inotify **W2** on the resolved output directory and run the normal
   harvest pipeline — from here the existing RECORDING → FINALISING → IDLE
   machinery applies unchanged (`metadata.yaml` `CLOSE_WRITE` or the inactivity
   fallback finalise it; recorder-process exit is an additional finalise hint).
3. The recording is **referenced in place**, never moved into the spool: the
   `Bag` entry records `source = "detected"` and `path` = the recording's real
   absolute path. It is copied into the crate at `mission_close` (the assembler
   ingests a foreign path the same way it moves a spool bag); if the source has
   moved or vanished by then, `mission_close` warns and `verify` catches it.

**Concurrency — one bag, one mission (DECIDED, PR #32).** If the watchdog is
already RECORDING (a spool *or* foreign bag is active), a newly detected foreign
recorder is queued and logged exactly like a second spool bag, and adopted only
once the active bag finalises. Foreign detection never bypasses the
single-active-bag invariant.

**Out of the poller's reach** — a recording finished before a scan saw it, made
while the watchdog was down, or copied from another machine — is handled by the
manual `ros2 fair adopt <bagdir>` escape hatch (see `specs/cli.md`), which runs
the same FINALISING processing and appends a `source = "adopted"` bag entry.

## Timeout summary

| Constant | Value | Where |
|---|---|---|
| `BAG_INACTIVITY_S` | 30 | RECORDING → FINALISING fallback |
| `FOREIGN_SCAN_INTERVAL_S` | 1 | recorder-process poll for foreign bags (a cmdline sweep per tick; cwd/fd/environ reads only on a match, so short recordings stay inside the window) |
| `RCLPY_TIMEOUT_S` | 5 | ros_descriptions |
| `DOCKER_TIMEOUT_S` | 10 | docker_info total |
| `PIP_TIMEOUT_S` | 30 | python_env: each pip subprocess |
| `HARDWARE_CMD_TIMEOUT_S` | 10 | hardware_devices: each command |
| `HARDWARE_TOTAL_TIMEOUT_S` | 60 | hardware_devices: wall-clock budget |
| `ROS2_CLI_TIMEOUT_S` | 20 | each ros2 subprocess call |
| `PARAM_DUMP_BUDGET_S` | 60 | all param dumps combined |
| `ROS_RETRY_INTERVAL_S` | 60 | re-harvest while RECORDING |

All defined once in `watchdog.py`, importable by tests.

## ROS 2 environment

The service has no login shell, so it does **not** inherit a sourced ROS 2
environment. Without `ros2` on `PATH` (and `AMENT_PREFIX_PATH`, `ROS_DISTRO`,
`RMW_IMPLEMENTATION`, `ROS_DOMAIN_ID`, …) the graph/description harvest captures
nothing and `ros_distro` is `None`. To avoid this, `ros2 fair setup` snapshots
the operator's ROS environment (variables prefixed `ROS_`/`AMENT_`/`RMW_`/
`COLCON_`, plus `PATH`, `LD_LIBRARY_PATH`, `PYTHONPATH`, `CMAKE_PREFIX_PATH`,
and the DDS profile vars) into `/etc/fair-ros/watchdog.env`, which the unit
loads via `EnvironmentFile=-` (optional, so a robot set up before this existed
still starts and watches the spool). Setup snapshots whatever environment it
runs under, so it must run from a shell that has ROS sourced **and can see the
robot** (`ros2 node list` lists its nodes — this also confirms the service, run
as root with the same env, will reach the graph). Because `sudo` typically
resets `PATH`/strips `ROS_*` (and `sudo -E` is often blocked by `secure_path`),
the reliable recipe is to become root and source ROS there:
`sudo su` → `source /opt/ros/<distro>/setup.bash` (+ the overlay/env that sets
`ROS_DOMAIN_ID`/`RMW_IMPLEMENTATION`) → `ros2 fair setup`. Setup warns if
`ROS_DISTRO` is absent. Re-run setup to refresh the snapshot after a distro or
DDS change.

## Error handling

| Condition | Behaviour |
|---|---|
| ROS 2 not running | ros2 subprocesses fail/time out → empty graph sections, `harvest_status.ros_graph = "failed"`, retry every 60 s while RECORDING. Never crash, never block the event loop (harvest runs in a worker thread). |
| Docker absent | `docker` binary missing or daemon down → `docker_containers = []`, status `skipped`. Silent. |
| pip absent / broken | `pip_version = None`, raw freeze/list omitted, `harvest_status.python_env = "partial"`; structured `packages` still captured from `importlib.metadata`. Never fatal. |
| Hardware command missing / permission denied / timeout | The offending command's results are skipped; other sources still populate `hardware_devices`. Status `partial` (or `skipped` if no command and no `/dev` device is available). `lsusb -v` and `dmesg` denials drop their raw artifacts only. |
| `robot_identity.yaml` missing/invalid | Log one warning per watchdog lifetime, `harvest_status.robot_identity = "failed"`, robot/sensors/calibrations sections empty. The miss surfaces to the user later at `mission_close` as: "This robot hasn't been set up yet — ask your engineer to run `ros2 fair setup`." |
| Spool partition full | Log error, keep watching; never delete anything. |
| Watchdog killed mid-harvest | Atomic writes mean `harvest.json` is either the old or the new version, never torn. |

## `watchdog.state` file

`/var/fair-ros/watchdog.state`, JSON, written atomically on every transition and
every 60 s heartbeat while RECORDING:

```json
{
  "version": 1,
  "pid": 1234,
  "state": "RECORDING",
  "since": "2026-06-12T14:03:00+00:00",
  "heartbeat_at": "2026-06-12T14:09:00+00:00",
  "active_bag_dir": "/var/fair-ros/spool/bags/rosbag2_2026_06_12-14_02_58",
  "last_bag_event_at": "2026-06-12T14:08:58+00:00",
  "harvest_status": {
    "robot_identity": "ok",
    "system_info": "ok",
    "python_env": "ok",
    "hardware_devices": "partial",
    "ros_graph": "ok",
    "ros_descriptions": "timeout",
    "docker_info": "skipped"
  }
}
```

`mission_status` reads this file (plus PID liveness check via `/proc/<pid>`) and
never talks to the watchdog directly.

## Restart recovery (statelessness)

On startup the watchdog trusts the **filesystem**, not the previous state file:

1. Scan `/var/fair-ros/spool/bags/*/`:
   - dir with storage files but **no** `metadata.yaml` → assume RECORDING; re-arm W2,
     re-run harvest if `harvest.json` is missing.
   - dir with `metadata.yaml` but no corresponding entry in `harvest.json.bags[]`
     → run FINALISING for it immediately.
   - all bags finalised or spool empty → IDLE.
2. Rewrite `watchdog.state` to reflect reality.

The previous state file is read only for logging ("resuming after restart, was
RECORDING").
