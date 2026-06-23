# fair-ros

[![CI](https://github.com/gdl-res/ROS-FAIRy/actions/workflows/ci.yml/badge.svg)](https://github.com/gdl-res/ROS-FAIRy/actions/workflows/ci.yml)

Make robotics field mission data **FAIR** (Findable, Accessible,
Interoperable, Reusable) with zero friction for the operator.

fair-ros works like a dashcam: a background watchdog notices when a rosbag
recording starts, silently captures the context around it (robot identity,
ROS graph, software versions, container digests, Python environment, connected
hardware, sensor health), and at the end of the mission asks the operator one
question: *save or discard?*

Saved missions become self-contained [RO-Crate](https://w3id.org/ro/crate)
archives — rosbags plus machine-readable JSON-LD metadata aligned to
schema.org and W3C SSN/SOSA — indexed locally in SQLite. No cloud, no
network, everything on the robot.

## Commands

> **Run `setup` from a root shell with ROS sourced and the robot graph visible.**
> The background watchdog runs as a system service with no login shell, so
> `setup` snapshots *your shell's* ROS environment for it. `sudo su` strips that
> environment, so source ROS *inside* the root shell and check `ros2 node list`
> first — `setup` now fails (rather than warning) if it can't capture a ROS
> environment or sees no nodes:
> ```
> sudo su
> source /opt/ros/<distro>/setup.bash   # + the overlay that sets ROS_DOMAIN_ID / RMW
> ros2 node list                        # must list your robot's nodes
> ros2 fair setup
> ```

```
ros2 fair setup            # one-time robot setup (engineer, needs sudo)
ros2 fair doctor           # preflight: is the robot ready to capture a mission?
ros2 fair mission_start    # 5-question briefing, under 2 minutes
ros2 fair mission_record   # record (wraps ros2 bag record)
ros2 fair mission_close    # review summary + data-quality verdict, save or discard
ros2 fair mission_status   # what is the assistant doing right now?
ros2 fair list             # table of saved missions
ros2 fair diff [A] [B]     # compare two missions, show only what changed
ros2 fair verify [M]       # check a saved archive is complete and unmodified
ros2 fair export [M]       # package a mission into one portable, checksummed file
ros2 fair repair [M]       # re-stamp bad-clock recordings so they play again
```

All verbs accept `--debug` for verbose logging to stderr. `mission_status`,
`list`, `diff`, `verify`, `doctor`, `export`, and `repair` accept `--json` for
machine-readable output (for scripts).

`mission_close` accepts `--note TEXT` to attach post-mission notes without
an interactive prompt.

### Safeguards

The tool actively prevents the field failures that produce useless data:

- **Preflight check** — `ros2 fair doctor` reports a single READY / NOT READY
  verdict: watchdog running, ROS reachable (from your shell *and* from the
  background service), the service's own ROS environment present and on the same
  `ROS_DOMAIN_ID` / `RMW_IMPLEMENTATION` as you, clock synchronised, `mcap`
  present, disk space, robot identity. Run it before a mission instead of
  discovering problems after.
- **Service-env reconciliation** — the watchdog starts from a frozen ROS-env
  snapshot taken at `setup`, which goes blind if you later record under a
  different domain or RMW. `mission_start` / `mission_record` hand the live
  recording shell's DDS discovery settings (domain, RMW) to the watchdog, so its
  background harvest always sees the same ROS graph the recorder does — no more
  empty-graph archives from env drift. (Only discovery keys are adopted from
  that group-writable handoff, never loader paths, so it can't be used to run
  code as the root service.)
- **Clock guardrail** — an unsynchronised system clock stamps messages near the
  epoch, making recordings unplayable. `mission_record` refuses to record on a
  bad clock unless you confirm; `mission_start` warns.
- **Degradation gate** — `mission_close` grades every mission `ok` / `degraded`
  / `poor`. A `poor` mission (no ROS context captured, or recordings with an
  unusable clock) makes saving a deliberate choice and is flagged in
  `ros2 fair list`, so a near-empty archive can't be saved by reflex.
- **Recovery** — `ros2 fair repair` writes playable copies of bad-clock
  recordings (re-stamped, regenerated `metadata.yaml`, originals untouched). See
  [docs/recovering-bad-clock-bags.md](docs/recovering-bad-clock-bags.md).

## What gets captured automatically

Every time a recording starts, the watchdog harvests:

| Source | What |
|---|---|
| `robot_identity.yaml` | Robot name, platform, serial number, declared sensors, calibration file paths |
| System info | Hostname, kernel, architecture, `$ROS_DISTRO` |
| **Python environment** | Interpreter path/version, all installed packages with version and editable-install flag, pip freeze snapshot |
| **Connected hardware** | USB devices (`lsusb`), PCI devices (`lspci`), video devices (`/dev/video*`, `v4l2-ctl`), serial devices (`/dev/ttyUSB*`, `/dev/ttyACM*`, `/dev/serial/by-id/*`), udev properties, filtered kernel messages |
| ROS graph | Node list, topic list with types, parameter dump, sensor liveness check |
| Docker containers | Image references, digests, Compose file snapshots |
| Robot description | URDF from `/robot_description`, TF static transforms |
| Bag health | Per-topic gap detection, low-rate warnings, plain-language summaries (sqlite3 and MCAP bags) |

All harvest steps are read-only, non-invasive, and timeout-safe. Missing
commands (e.g. no `lsusb`) produce partial results, never failures.

## Archive layout

Each saved mission is a self-contained directory:

```
2026-06-12_14-02-58_marsh-creek-north-bank_jane-doe/
├── ro-crate-metadata.json     # JSON-LD (RO-Crate 1.1 + schema.org + SSN/SOSA)
├── mission_record.json        # full structured record (machine-readable)
├── README.md                  # plain-language summary
├── bags/                      # rosbag2 recordings (moved, not copied; per-file sha256 recorded)
├── harvest/
│   ├── harvest.json           # raw harvest data
│   ├── robot_description.urdf
│   ├── tf_static.json
│   ├── pip_freeze.txt         # Python package snapshot
│   ├── lsusb_verbose.txt      # USB descriptor dump
│   └── dmesg_usb.txt          # filtered kernel hardware messages
├── calibrations/              # sensor calibration files (copied + sha256)
└── docker/                    # container inventory + Compose snapshots
```

## Roadmap

**Working today**
- Dashcam watchdog → briefing → save/discard → RO-Crate archive → SQLite index
- Automatic harvest: robot identity, ROS graph, software versions, Docker
  digests, Python environment, connected hardware, robot description
- Bag health (gaps, low-rate, never-published) on **both sqlite3 and MCAP**
  storage, with distro-aware defaults and pluggable storage readers, plus
  recording-window recovery from a corrupted bag clock
- `ros2 fair doctor` — preflight readiness check (incl. whether the background
  service, not just your shell, can reach ROS)
- Clock guardrail before recording + data-quality gate at save time
- `ros2 fair verify` — schema, RO-Crate JSON-LD, referenced files,
  **per-file bag checksums + calibration checksums**, and index registration
- `ros2 fair export` — one portable, `sha256`-checksummed bundle per mission
- `ros2 fair repair` — make bad-clock recordings playable again (non-destructive)
- CI gate: ruff + mypy + pytest across Python 3.10–3.13

**Ready, needs a real robot to exercise**
- Real-bag fixtures — drop bags from `ros2 bag record` into `tests/fixtures/`
  to validate the core against real Jazzy metadata
  ([how](tests/fixtures/README.md))
- Live-ROS smoke tests — `pytest -m ros` on a sourced Jazzy box validates
  plugin registration and the full live pipeline
  ([how](docs/real-robot-smoke-test.md))

**Out of scope (v1)**
- Cloud sync / remote registry, web UI, multi-robot session tracking
- Automatic environment data from external APIs (weather, map tiles)
- Migration tooling for ROS 2 distros other than Jazzy

## Documentation

- `CLAUDE.md` — project constitution: design principles, layout, implementation rules
- `specs/` — authoritative sub-specifications per component:
  - `specs/data_model.md` — every field in a MissionRecord
  - `specs/watchdog.md` — inotify state machine, harvest pipeline, timeouts
  - `specs/cli.md` — exact behaviour of every `ros2 fair` verb
  - `specs/archive.md` — failure-safe assembly algorithm, SQLite index
  - `specs/ro_crate_schema.md` — JSON-LD entity mapping

## Development

```bash
pip install -e '.[dev]'         # runtime deps + pytest, rocrate, ruff, mypy
python3 -m pytest tests/        # no robot or live ROS graph required
ruff check .                    # lint (enforced in CI)
mypy fair_ros                   # type check (enforced in CI)
```

Optional live-ROS smoke tests (deselected by default) validate plugin
registration and the real harvest/record/verify pipeline on a sourced ROS 2
box: `pytest -m ros -v` — see [docs/real-robot-smoke-test.md](docs/real-robot-smoke-test.md).

Requires Python ≥ 3.10, pydantic ≥ 2.5, rich ≥ 13, PyYAML, inotify_simple, and
`mcap` (MCAP is rosbag2's default storage from Jazzy on; it enables bag timing,
health analysis, and repair). The code still degrades gracefully if `mcap` is
somehow absent.

Licensed under the [Apache License 2.0](LICENSE).
