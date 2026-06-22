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
processed after the current one finalises; the overlap is logged.

### IDLE → RECORDING

Trigger: first storage file (`.db3`/`.mcap`) created inside a new bag directory under W1.
(`metadata.yaml` alone does not trigger — rosbag2 writes it only on close.)

On entry, run the harvest pipeline **in this order** (cheap and local first, so a
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

## Timeout summary

| Constant | Value | Where |
|---|---|---|
| `BAG_INACTIVITY_S` | 30 | RECORDING → FINALISING fallback |
| `RCLPY_TIMEOUT_S` | 5 | ros_descriptions |
| `DOCKER_TIMEOUT_S` | 10 | docker_info total |
| `PIP_TIMEOUT_S` | 30 | python_env: each pip subprocess |
| `HARDWARE_CMD_TIMEOUT_S` | 10 | hardware_devices: each command |
| `HARDWARE_TOTAL_TIMEOUT_S` | 60 | hardware_devices: wall-clock budget |
| `ROS2_CLI_TIMEOUT_S` | 20 | each ros2 subprocess call |
| `PARAM_DUMP_BUDGET_S` | 60 | all param dumps combined |
| `ROS_RETRY_INTERVAL_S` | 60 | re-harvest while RECORDING |

All defined once in `watchdog.py`, importable by tests.

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
