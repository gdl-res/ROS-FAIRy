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

```
ros2 fair setup            # one-time robot setup (engineer, needs sudo)
ros2 fair mission_start    # 5-question briefing, under 2 minutes
ros2 fair mission_record   # record (wraps ros2 bag record)
ros2 fair mission_close    # review summary, then save or discard
ros2 fair mission_status   # what is the assistant doing right now?
ros2 fair list             # table of saved missions
ros2 fair diff [A] [B]     # compare two missions, show only what changed
```

All verbs accept `--debug` for verbose logging to stderr. `mission_status`,
`list`, and `diff` accept `--json` for machine-readable output (for scripts).

`mission_close` accepts `--note TEXT` to attach post-mission notes without
an interactive prompt.

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
2026-06-12_marsh-creek-north-bank_jane-doe/
├── ro-crate-metadata.json     # JSON-LD (RO-Crate 1.1 + schema.org + SSN/SOSA)
├── mission_record.json        # full structured record (machine-readable)
├── README.md                  # plain-language summary
├── bags/                      # rosbag2 recordings (moved, not copied)
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

Requires Python ≥ 3.10, pydantic ≥ 2.5, rich ≥ 13, PyYAML, inotify_simple.
The `mcap` package is an optional extra (in `[test]`/`[dev]`) that enables
timestamp-level health analysis of MCAP bags; without it, MCAP bags get
metadata-level checks only.

Licensed under the [Apache License 2.0](LICENSE).
