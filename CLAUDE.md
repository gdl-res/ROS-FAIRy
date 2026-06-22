# FAIR-ROS — Project Constitution

## What This Project Is

`fair-ros` is a ROS 2 tool that makes robotics field mission data **FAIR-compliant**
(Findable, Accessible, Interoperable, Reusable) with zero friction for the operator.

The operator may not be a robotics engineer. The system must work like a dashcam:
always recording context silently in the background, asking the user as little as
possible, and presenting a single save/discard decision at the end of a mission.

The tool surfaces as a native ROS 2 CLI extension: `ros2 fair <command>`.

---

## Design Principles (never violate these)

1. **Dashcam model** — the system is always on. The user never has to think about
   starting context capture. It happens automatically when bags are detected.

2. **Plain language everywhere** — every message the user sees must be readable by
   a non-engineer. No ROS jargon, no technical IDs, no stack traces in normal flow.

3. **Tiered confidence** — every metadata field is tagged:
   - 🟢 AUTO: discovered silently, no user involvement
   - 🔴 REQUIRED: cannot be inferred, user must answer at `mission_start`
   There are no 🟡 ambiguous fields presented to the user.

4. **Robot-agnostic** — nothing in the codebase assumes a specific robot, sensor
   suite, or topic list. All robot-specific knowledge lives in
   `/etc/fair-ros/robot_identity.yaml`, written once by an engineer at setup time.

5. **Version-agnostic core** — all ROS 2 interaction that can be done via subprocess
   (`ros2 node list`, `ros2 topic list`, `ros2 param dump`) MUST use subprocess, not
   `rclpy` introspection. `rclpy` is only used where subprocess cannot substitute
   (e.g. subscribing to `/robot_description`, `/tf_static`). This keeps the core
   portable across ROS 2 distros.

6. **Local-first** — no network calls, no cloud dependencies. Everything lives on
   the robot's filesystem. The SQLite index and all mission archives are local.

---

## Repository Layout

```
fair-ros/
├── CLAUDE.md                        ← you are here
├── README.md
├── specs/                           ← detailed sub-specifications (see below)
│   ├── data_model.md
│   ├── watchdog.md
│   ├── cli.md
│   ├── archive.md
│   └── ro_crate_schema.md
├── fair_ros/                        ← main Python package
│   ├── __init__.py
│   ├── command/                     ← ros2cli verb plugin
│   │   ├── __init__.py
│   │   └── fair.py                  ← entry point, routes subcommands
│   ├── subcommands/                 ← one file per ros2 fair <cmd>
│   │   ├── setup.py
│   │   ├── mission_start.py
│   │   ├── mission_record.py
│   │   ├── mission_close.py
│   │   ├── mission_status.py
│   │   ├── mission_diff.py          ← compare two missions (ros2 fair diff)
│   │   ├── verify.py                ← integrity-check a saved archive (ros2 fair verify)
│   │   └── list_missions.py
│   ├── harvest/                     ← auto-discovery subsystem
│   │   ├── ros_graph.py             ← nodes, topics, params via subprocess
│   │   ├── ros_descriptions.py      ← /robot_description, /tf_static via rclpy
│   │   ├── docker_info.py           ← image digests, compose snapshot
│   │   ├── system_info.py           ← hostname, arch, kernel
│   │   ├── python_env.py            ← interpreter, installed packages, pip freeze
│   │   ├── hardware_devices.py      ← USB/PCI/video/serial devices, udev props
│   │   └── robot_identity.py        ← reads /etc/fair-ros/robot_identity.yaml
│   ├── watchdog/
│   │   ├── watchdog.py              ← inotify watcher on spool/bags/
│   │   └── fair-ros-watchdog.service ← systemd unit template
│   ├── manifest/
│   │   ├── builder.py               ← merges harvest + user input → manifest
│   │   ├── validator.py             ← checks required fields are present
│   │   └── schema.py                ← Pydantic models for all record types
│   ├── archive/
│   │   ├── assembler.py             ← builds RO-Crate directory structure
│   │   ├── ro_crate.py              ← writes ro-crate-metadata.json (JSON-LD)
│   │   └── index.py                 ← SQLite mission index (read/write)
│   ├── ui/
│   │   ├── briefing.py              ← interactive mission_start wizard (rich TUI)
│   │   ├── review.py                ← mission_close summary + confirm/discard
│   │   ├── diff.py                  ← mission diff rendering
│   │   └── status.py                ← mission_status display
│   └── utils/
│       ├── paths.py                 ← canonical paths (spool, archive, config)
│       ├── fsio.py                  ← atomic JSON writes, directory sizing
│       ├── bag_storage.py           ← pluggable rosbag2 storage readers (sqlite3 + mcap)
│       ├── ros_distro.py            ← distro detection + per-distro capabilities (default storage)
│       └── topic_health.py          ← gap detection on topic timestamps
├── systemd/
│   └── fair-ros-watchdog.service
├── tests/
│   ├── unit/
│   └── integration/
├── setup.py
└── package.xml
```

---

## Sub-Specifications

Before implementing any component, read its spec file in `specs/`. These are the
authoritative source of truth for behaviour, schema, and edge cases. CLAUDE.md is
the map; spec files are the territory.

| Component | Spec file |
|---|---|
| Data model & field list | `specs/data_model.md` |
| Watchdog service | `specs/watchdog.md` |
| CLI commands | `specs/cli.md` |
| Archive & RO-Crate | `specs/archive.md` |
| RO-Crate JSON-LD schema | `specs/ro_crate_schema.md` |

---

## Key Paths (canonical, defined in `utils/paths.py`)

| Purpose | Path |
|---|---|
| Robot identity config | `/etc/fair-ros/robot_identity.yaml` |
| Active spool (bags + harvest) | `/var/fair-ros/spool/` |
| Mission archives | `/var/fair-ros/archive/` |
| SQLite index | `/var/fair-ros/index.db` |
| Watchdog PID / state | `/var/fair-ros/watchdog.state` |

All paths must be read from `utils/paths.py`. Never hardcode them elsewhere.

---

## Data Model Overview

A **Mission Record** has the following top-level sections. Full field definitions
are in `specs/data_model.md`.

```
MissionRecord
├── identity          # mission ID, timestamp, operator name
├── intent            # human description of goal, location, environment
├── robot             # from robot_identity.yaml — platform, serial, owner
├── sensors[]         # from robot_identity.yaml + runtime health check
├── software          # ROS 2 distro, package versions, docker digests
├── ros_graph         # nodes, topics, params snapshot at mission start
├── calibrations[]    # paths to cal files linked from robot_identity.yaml
├── bags[]            # paths, sizes, duration, topic health report
├── hardware_devices[] # USB/PCI/video/serial devices discovered at mission start
└── provenance        # fair-ros version, harvest timestamp, confidence tags
```

Every field carries a `confidence` tag: `"auto"` or `"user"`.

---

## Component Responsibilities

### Watchdog (`watchdog/`)
- Runs as a systemd service, always active
- Monitors `/var/fair-ros/spool/bags/` via inotify for `.db3` file creation
- On detection: calls `harvest/` modules, writes `harvest.json` to spool
- On bag close (no new writes for N seconds): finalises harvest, updates state
- Must be stateless across restarts — reads state from `watchdog.state`
- Must NOT block or crash if ROS 2 is not running; log and retry silently

### Harvest (`harvest/`)
- Each module is independently callable and returns a typed dict
- `ros_graph.py` uses subprocess only — `ros2 node list`, `ros2 topic list`,
  `ros2 param dump --all`, `ros2 pkg list`
- `ros_descriptions.py` spins a minimal rclpy node with a 5-second timeout to
  grab `/robot_description` and `/tf_static`; returns empty dict on timeout
- `docker_info.py` runs `docker inspect` on all running containers; graceful
  no-op if Docker is not available
- `python_env.py` captures the interpreter, installed packages (with editable
  flags) and a pip-freeze snapshot via subprocess; partial result, never fails
- `hardware_devices.py` enumerates USB/PCI/video/serial devices and udev
  properties; missing tools (e.g. no `lsusb`) yield partial results, not errors
- `topic_health.py` post-processes bag metadata to detect topic gaps > 1 second

### CLI (`subcommands/`)
- `setup.py` — interactive wizard (rich TUI), installs systemd service, creates
  `/etc/fair-ros/robot_identity.yaml` interactively, one-time per robot
- `mission_start.py` — briefing wizard, writes `mission_context.json` to spool,
  5 questions max, plain language, Enter to skip optional fields
- `mission_record.py` — validates spool is ready, calls `ros2 bag record` as a
  subprocess to `/var/fair-ros/spool/bags/`, streams output to terminal
- `mission_close.py` — reads spool + harvest, shows plain-language summary with
  warnings, asks "Save this mission? [Y/n]", calls assembler if yes
- `mission_status.py` — shows current watchdog state, spool size, active bags
- `list_missions.py` — queries SQLite index, shows table of past missions
- `mission_diff.py` — compares two missions and shows only what changed
- `verify.py` — re-checks a saved archive (schema, RO-Crate JSON-LD, referenced
  files present, bag data files present, calibration checksums, index entry);
  read-only, plain-language PASS/FAIL
  (`mission_status`, `list`, `diff`, and `verify` accept `--json` for scripts)

### Manifest Builder (`manifest/`)
- `builder.py` merges `harvest.json` + `mission_context.json` → `MissionRecord`
- All Pydantic models in `schema.py`; validation in `validator.py`
- Missing required fields cause a clear, plain-language error — never a traceback

### Archive (`archive/`)
- `assembler.py` creates the RO-Crate directory under `/var/fair-ros/archive/`
- Directory name format: `YYYY-MM-DD_<sanitised-location>_<operator>/`
- `ro_crate.py` writes `ro-crate-metadata.json` as JSON-LD aligned to:
  - RO-Crate 1.1 spec
  - schema.org/Dataset
  - W3C SSN/SOSA for sensor descriptions
- `index.py` inserts a row into SQLite after successful archive

---

## ROS 2 CLI Plugin Registration

The `fair` verb must be registered in `setup.py` under:

```python
entry_points={
    'ros2cli.command': [
        'fair = fair_ros.command.fair:FairCommand',
    ],
}
```

Subcommands register under:
```python
    'fair.verb': [
        'setup = fair_ros.subcommands.setup:SetupVerb',
        'mission_start = fair_ros.subcommands.mission_start:MissionStartVerb',
        'mission_record = fair_ros.subcommands.mission_record:MissionRecordVerb',
        'mission_close = fair_ros.subcommands.mission_close:MissionCloseVerb',
        'mission_status = fair_ros.subcommands.mission_status:MissionStatusVerb',
        'list = fair_ros.subcommands.list_missions:ListVerb',
        'diff = fair_ros.subcommands.mission_diff:DiffVerb',
        'verify = fair_ros.subcommands.verify:VerifyVerb',
    ],
```

---

## UI / UX Rules

- Use `rich` for all terminal output — panels, tables, progress bars, prompts
- Never print raw Python dicts or JSON to the user
- Warnings in the `mission_close` summary are generated automatically from
  `topic_health.py`; show them in plain English: "GPS signal was lost for 4 minutes"
  not "topic /fix had a gap of 243.2s at t=1718194980.1"
- Confirmations default to Yes (`[Y/n]`); destructive actions default to No (`[y/N]`)
- The briefing wizard must complete in under 2 minutes for a typical operator

---

## Testing Strategy

- Unit tests for all `harvest/` and `manifest/` modules with mocked subprocess
  output and fixture files
- Integration tests spin a minimal ROS 2 environment (rosbag play on a test bag)
- The watchdog is tested with a mock inotify event injector
- No test should require a physical robot or live ROS graph

---

## Implementation Order

Implement in this order; each phase is independently testable:

1. `utils/paths.py` + `harvest/` modules (foundation, no ROS deps except subprocess)
2. `manifest/schema.py` + `manifest/builder.py` + `manifest/validator.py`
3. `watchdog/watchdog.py` + systemd unit
4. `archive/assembler.py` + `archive/ro_crate.py` + `archive/index.py`
5. `ui/` modules (rich TUI)
6. `subcommands/` wiring everything together
7. `command/fair.py` ROS 2 CLI registration

Do not skip ahead. Each phase builds on the previous one.

---

## Out of Scope (v1)

- Cloud sync or remote registry
- Web UI (CLI only for v1)
- Multi-robot session tracking
- Automatic environment data from external APIs (weather, map tiles)
- Migration tooling for ROS 2 distros other than Jazzy (structure is ready, not implemented)

