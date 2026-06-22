# Spec: Data Model

Authoritative definition of every field in a `MissionRecord`. The Pydantic models in
`fair_ros/manifest/schema.py` must match this document exactly.

## Conventions

- **Confidence**: `auto` (harvested silently) or `user` (typed by the operator).
  There is no third tier. Serialized records expose confidence via
  `provenance.field_confidence` (see below), keeping the data fields themselves clean.
- **Required**: a record cannot be archived without this field. Validation failures
  produce plain-language errors naming the missing information, never a traceback.
- **Timestamps**: ISO 8601 with explicit UTC offset (`2026-06-12T14:03:00+00:00`).
- **Source** column names the harvest module function or briefing question that
  produces the field.
- All models are Pydantic v2, `model_config = ConfigDict(extra="forbid")`.
- `schema_version` for this document: `"1.0"`.

## Top-level structure

```
MissionRecord
├── schema_version:    str          # literal "1.0"
├── identity:          Identity
├── intent:            Intent
├── robot:             Robot
├── sensors:           list[Sensor]
├── software:          Software
├── ros_graph:         RosGraph
├── calibrations:      list[Calibration]
├── bags:              list[Bag]
├── hardware_devices:  list[HardwareDevice]
└── provenance:        Provenance
```

`hardware_devices`, `software.python_env`, and their sub-models (below) were
added after the initial `schema_version "1.0"` release. They are additive and
optional (empty-list / `None` defaults), so records written before they existed
still validate; `schema_version` stays `"1.0"`.

---

## `identity`

| Field | Type | Req | Conf | Source | Description |
|---|---|---|---|---|---|
| `mission_id` | `str` | yes | auto | `manifest/builder.py` | `m-YYYYMMDD-HHMMSS-xxxx` where `xxxx` is 4 lowercase hex chars from `uuid4`. Generated when `mission_context.json` is first written (at `mission_start`), or at `mission_close` if the operator skipped the briefing. |
| `created_at` | `datetime` | yes | auto | builder | Time the mission context was created (UTC). |
| `operator_name` | `str` | **yes** | user | Briefing Q1 | Free text, 1–80 chars, stripped. |
| `operator_contact` | `str \| None` | no | user | Setup-time default | Defaults to `robot_identity.yaml → owner.contact_email`; the operator is never asked for it. |

## `intent`

| Field | Type | Req | Conf | Source | Description |
|---|---|---|---|---|---|
| `goal` | `str` | **yes** | user | Briefing Q2 | One-sentence mission goal, 1–280 chars. |
| `location_name` | `str` | **yes** | user | Briefing Q3 | Human place name ("Marsh Creek, north bank"). Also used (sanitised) in the archive directory name. |
| `environment` | `str \| None` | no | user | Briefing Q4 | Free text or one of the suggested choices (`outdoor`, `indoor`, `underground`, `marine`, `aerial`). Skippable. |
| `notes` | `str \| None` | no | user | Briefing Q5 | Anything else. Skippable. |

## `robot`

All fields come from `harvest/robot_identity.py`, which is a thin validated reader of
`/etc/fair-ros/robot_identity.yaml` (written once by `ros2 fair setup`).

| Field | Type | Req | Conf | Source | Description |
|---|---|---|---|---|---|
| `name` | `str` | yes | auto | identity yaml `robot.name` | Friendly robot name ("Heron-02"). |
| `platform` | `str` | yes | auto | yaml `robot.platform` | Make + model ("Clearpath Heron USV"). |
| `serial_number` | `str` | yes | auto | yaml `robot.serial_number` | Manufacturer serial or asset tag. |
| `owner_organization` | `str` | yes | auto | yaml `owner.organization` | Owning institution. |
| `owner_contact` | `str` | yes | auto | yaml `owner.contact_email` | Steward email; validated at setup time. |

## `sensors[]`

Static description from `robot_identity.yaml → sensors[]`, enriched at harvest time
with a liveness check (topic present in `ros2 topic list` output).

| Field | Type | Req | Conf | Source | Description |
|---|---|---|---|---|---|
| `sensor_id` | `str` | yes | auto | yaml | Stable slug unique within the robot ("gps0", "lidar_front"). |
| `type` | `str` | yes | auto | yaml | Plain category: `gps`, `lidar`, `camera`, `imu`, `sonar`, `other`. Maps to SSN/SOSA in the crate (see `ro_crate_schema.md`). |
| `make_model` | `str` | yes | auto | yaml | "u-blox ZED-F9P". |
| `topic` | `str` | yes | auto | yaml | Primary ROS topic the sensor publishes. |
| `frame_id` | `str \| None` | no | auto | yaml | TF frame, if known. |
| `calibration_ref` | `str \| None` | no | auto | yaml | `Calibration.name` this sensor uses, if any. Must match an entry in `calibrations[]`. |
| `detected_at_start` | `bool` | yes | auto | `harvest/ros_graph.py` | True if `topic` appeared in the topic list during harvest. False is not an error; it becomes a plain-language warning at `mission_close`. |

## `software`

| Field | Type | Req | Conf | Source | Description |
|---|---|---|---|---|---|
| `ros_distro` | `str \| None` | no | auto | `harvest/system_info.py` (`$ROS_DISTRO`) | e.g. `"jazzy"`. `None` if ROS env not sourced when harvested. |
| `ros_packages` | `list[str]` | yes | auto | `harvest/ros_graph.py` (`ros2 pkg list`) | Sorted package names visible on the path. Empty list if ROS unavailable. |
| `apt_ros_versions` | `dict[str, str]` | yes | auto | system_info (`dpkg-query -W ros-*`) | Debian package → version for installed `ros-<distro>-*` packages. Empty dict on non-Debian systems. |
| `docker_containers` | `list[DockerContainer]` | yes | auto | `harvest/docker_info.py` | Empty list when Docker is absent or has no running containers. |
| `fair_ros_version` | `str` | yes | auto | `fair_ros.__version__` | Version of this tool at harvest time. |
| `python_env` | `PythonEnv \| None` | no | auto | `harvest/python_env.py` | The Python runtime fair-ros ran in and its installed packages. `None` only if the module raised unexpectedly; a broken pip still yields a partial `PythonEnv`. |

### `DockerContainer`

| Field | Type | Description |
|---|---|---|
| `name` | `str` | Container name. |
| `image` | `str` | Image reference as run (`org/img:tag`). |
| `digest` | `str \| None` | First `RepoDigests` entry; `None` for locally built, never-pushed images. |
| `compose_project` | `str \| None` | From label `com.docker.compose.project`. |
| `compose_file` | `str \| None` | From label `com.docker.compose.project.config_files`; the file is snapshotted into the archive (see `specs/archive.md`). |

### `PythonEnv`

Captured by `harvest/python_env.py` using `sys`, `importlib.metadata`, and
best-effort subprocess `pip` calls to the current interpreter. Structured fields
come from `importlib.metadata` (always available); pip is only consulted for the
raw freeze/list artifacts and to fill `installer`/`location` gaps. All `auto`.

| Field | Type | Req | Source | Description |
|---|---|---|---|---|
| `executable` | `str` | yes | `sys.executable` | Absolute path to the interpreter fair-ros ran under. |
| `version` | `str` | yes | `sys.version` | Full interpreter version string. |
| `venv_path` | `str \| None` | no | `$VIRTUAL_ENV`, else `sys.prefix` when not `/usr` or `/usr/local` | Active virtual environment root; `None` for the system interpreter. |
| `pip_version` | `str \| None` | no | `importlib.metadata.version("pip")` | `None` when pip is absent. |
| `packages` | `list[PythonPackage]` | yes | `importlib.metadata.distributions()` | All installed distributions, sorted by normalised name. |
| `fair_ros_editable` | `bool` | yes | `direct_url.json` of the `fair_ros` dist | Whether this tool itself runs from an editable install. `False` in production. |
| `sys_path` | `list[str]` | yes | `sys.path` | Module search path at harvest time. |

The text of `pip freeze` and `pip list --format=json` are **not** fields here;
they are stored in `harvest.json → raw_python_env` and extracted to
`harvest/pip_freeze.txt` by the assembler (see `specs/archive.md`).

#### `PythonPackage`

| Field | Type | Description |
|---|---|---|
| `name` | `str` | Distribution name. |
| `version` | `str` | Installed version. |
| `installer` | `str \| None` | From the dist-info `INSTALLER` file (`pip`, `conda`, `apt`, …); `None` when absent. |
| `editable` | `bool` | `True` for editable/develop installs (PEP 610 `direct_url.json` or pip's `editable_project_location`). |
| `location` | `str \| None` | Source directory for editable/local installs; `None` otherwise. |

## `ros_graph`

Snapshot taken when the watchdog enters RECORDING (see `specs/watchdog.md`).

| Field | Type | Req | Conf | Source | Description |
|---|---|---|---|---|---|
| `captured_at` | `datetime \| None` | no | auto | `harvest/ros_graph.py` | `None` if ROS was never reachable. |
| `nodes` | `list[str]` | yes | auto | `ros2 node list` | Fully-qualified node names. Empty if unreachable. |
| `topics` | `list[TopicInfo]` | yes | auto | `ros2 topic list -t` | `TopicInfo = {name: str, type: str}`. |
| `parameters` | `dict[str, dict]` | yes | auto | `ros2 param dump <node>` per node | Node name → parameter tree. Nodes that fail to dump are simply absent. |
| `robot_description` | `str \| None` | no | auto | `harvest/ros_descriptions.py` | URDF XML string, `None` on 5 s timeout. Stored as a separate file in the archive; the manifest holds only its relative path after assembly. |
| `tf_static` | `list[dict] \| None` | no | auto | ros_descriptions | Serialized static transforms, `None` on timeout. |
| `complete` | `bool` | yes | auto | watchdog | False if any subprocess call failed; triggers a warning at close. |

## `calibrations[]`

| Field | Type | Req | Conf | Source | Description |
|---|---|---|---|---|---|
| `name` | `str` | yes | auto | yaml `calibrations[].name` | Unique slug ("camera_front_intrinsics"). |
| `source_path` | `str` | yes | auto | yaml | Absolute path on the robot at harvest time. |
| `archived_path` | `str \| None` | no | auto | `archive/assembler.py` | Relative path inside the crate after copy (`calibrations/<file>`); `None` before archiving or if the source file was missing. |
| `sha256` | `str \| None` | no | auto | assembler | Hash of the copied file. |
| `format` | `str \| None` | no | auto | yaml | e.g. `yaml`, `json`, `kalibr`. |

A `source_path` that no longer exists at archive time is a warning, not a failure.

## `bags[]`

One entry per rosbag2 recording directory found in the spool. Produced in the
watchdog FINALISING step from rosbag2's own `metadata.yaml` plus
`utils/topic_health.py`.

| Field | Type | Req | Conf | Source | Description |
|---|---|---|---|---|---|
| `path` | `str` | yes | auto | watchdog | Relative path inside the crate after assembly (`bags/<dir>`); spool-absolute before. |
| `storage_format` | `str` | yes | auto | bag `metadata.yaml` | `sqlite3` or `mcap`. When `metadata.yaml` omits it, inferred from the recording distro via `utils/ros_distro.default_storage()` (Jazzy+ → `mcap`, Foxy–Iron → `sqlite3`). |
| `size_bytes` | `int` | yes | auto | filesystem | Total directory size. |
| `start_time` | `datetime` | yes | auto | bag metadata | First message time. |
| `end_time` | `datetime` | yes | auto | bag metadata | Last message time. |
| `duration_s` | `float` | yes | auto | bag metadata | |
| `message_count` | `int` | yes | auto | bag metadata | |
| `topics` | `list[BagTopic]` | yes | auto | bag metadata | `BagTopic = {name, type, message_count, avg_frequency_hz}`. |
| `health_warnings` | `list[HealthWarning]` | yes | auto | `utils/topic_health.py` | Empty list = healthy. |

### `HealthWarning`

| Field | Type | Description |
|---|---|---|
| `topic` | `str` | Affected topic. |
| `sensor_id` | `str \| None` | Back-reference to `sensors[]` when the topic matches a declared sensor. |
| `kind` | `str` | `gap` (silence > 1 s mid-recording), `never_published` (declared sensor topic absent from bag), `low_rate` (avg rate < 25 % of other periods). |
| `start_offset_s` | `float \| None` | Seconds from bag start where the problem began. |
| `duration_s` | `float \| None` | Length of the gap. |
| `plain_text` | `str` | Pre-rendered sentence shown to the user: "GPS signal was lost for 4 minutes, starting 12 minutes in." UI layers must show this string, never the raw numbers. |

## `hardware_devices[]`

Connected sensors and hardware devices discovered by `harvest/hardware_devices.py`.
Read-only and non-invasive: every external command has a timeout, missing
binaries and permission-denied results yield a partial harvest, never a failure.
Empty list is always valid and never a validation requirement. All `auto`.

| Field | Type | Req | Source | Description |
|---|---|---|---|---|
| `device_class` | `str \| None` | no | inferred | `usb`, `pci`, `video`, `serial`. |
| `vendor_id` | `str \| None` | no | `lsusb` / udev | 4-char lowercase hex. |
| `product_id` | `str \| None` | no | `lsusb` / udev | 4-char lowercase hex. |
| `vendor_name` | `str \| None` | no | `lsusb` / `lspci` / udev | Human vendor name. |
| `product_name` | `str \| None` | no | `lsusb` / `lspci` / udev / by-id | Product description. |
| `serial_number` | `str \| None` | no | `lsusb -v` / udev `ID_SERIAL_SHORT` | **Potentially identifying** — allowed but flagged as such in the archive README. |
| `device_path` | `str \| None` | no | `/dev/*` glob / udev | e.g. `/dev/ttyUSB0`, `/dev/video0`. |
| `bus_path` | `str \| None` | no | `lsusb` / `lspci` | e.g. `Bus 002 Device 003`. |
| `driver` | `str \| None` | no | udev `ID_DRIVER` | Bound kernel module. |
| `source_command` | `str` | yes | harvest code | Origin of the entry: `lsusb`, `lspci`, `v4l2-ctl`, `glob:/dev/ttyUSB*`, etc. |
| `udev_properties` | `dict[str, str] \| None` | no | `udevadm info` | **Whitelisted keys only** (`DEVNAME`, `DEVTYPE`, `SUBSYSTEM`, `ID_BUS`, `ID_VENDOR`, `ID_VENDOR_ID`, `ID_MODEL`, `ID_MODEL_ID`, `ID_SERIAL_SHORT`, `ID_USB_CLASS`, `ID_USB_SUBCLASS`, `ID_DRIVER`, `ID_PATH`, `MAJOR`, `MINOR`); any other key is dropped. |

**Privacy rule:** the module never collects secrets, Wi-Fi/SSH credentials, user
files, wholesale environment variables, or home-directory contents. The raw
`lsusb -v` dump and filtered `dmesg` lines go to `harvest.json → raw_hardware`
and are extracted to `harvest/lsusb_verbose.txt` / `harvest/dmesg_usb.txt` by the
assembler — never into the manifest.

## `provenance`

| Field | Type | Req | Conf | Source | Description |
|---|---|---|---|---|---|
| `fair_ros_version` | `str` | yes | auto | package | Duplicated from `software` deliberately: survives even if `software` harvest failed. |
| `schema_version` | `str` | yes | auto | builder | `"1.0"`. |
| `harvested_at` | `datetime \| None` | no | auto | watchdog | When `harvest.json` was finalised. |
| `assembled_at` | `datetime \| None` | no | auto | assembler | When the crate was written. |
| `hostname` | `str` | yes | auto | system_info | |
| `kernel` | `str` | yes | auto | system_info (`uname -sr`) | |
| `arch` | `str` | yes | auto | system_info (`uname -m`) | |
| `field_confidence` | `dict[str, str]` | yes | auto | builder | Flat map of dotted field path → `"auto"` \| `"user"`, e.g. `"intent.goal": "user"`. Covers every populated leaf field. This is the single machine-readable source of confidence tags. |
| `harvest_status` | `dict[str, str]` | yes | auto | watchdog | Per harvest module: `ok`, `failed`, `skipped`, `timeout`, or `partial`. Module keys: `robot_identity`, `system_info`, `python_env`, `hardware_devices`, `ros_graph`, `ros_descriptions`, `docker_info`. `partial` (used by `python_env` and `hardware_devices`) means some sub-commands succeeded and others were missing, timed out, or permission-denied. The canonical key list is `manifest/builder.HARVEST_MODULES`. |

---

## Intermediate files (spool)

Two JSON files in `/var/fair-ros/spool/` feed `manifest/builder.py`:

- **`harvest.json`** — written by the watchdog. Contains `robot`, `sensors`,
  `software` (including `python_env`), `ros_graph`, `hardware_devices`, `bags`
  (added at FINALISING), and the `provenance` harvest fields. All `auto`. It also
  carries raw-artifact keys consumed only by the assembler, never by the
  manifest: `raw_docker_inspect`, `raw_python_env` (`pip_freeze`,
  `pip_list_json`), and `raw_hardware` (`lsusb_verbose`, `dmesg_usb`).
- **`mission_context.json`** — written by `mission_start`. Contains `identity`
  (id, created_at, operator) and `intent`. All `user` except the generated id.

`builder.py` merges the two, fills `field_confidence`, and returns a `MissionRecord`.
`validator.py` then checks the three required user fields (`operator_name`, `goal`,
`location_name`) and the presence of at least one bag. Anything else missing is a
warning, never a validation failure — the dashcam must not lose data over missing
nice-to-haves.
