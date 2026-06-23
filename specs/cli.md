# Spec: CLI Commands

Exact behaviour of every `ros2 fair <verb>`. All terminal output uses `rich`
(panels, tables, prompts). Exit codes: `0` success, `1` user-facing failure
(plain-language message, no traceback), `130` on Ctrl-C.

Global rules:
- Plain language only. Never show topic names, exception text, or JSON to the
  operator in the normal flow. A `--debug` flag on every verb enables verbose
  logging to stderr for engineers.
- Confirmations default Yes `[Y/n]`; destructive actions default No `[y/N]`.
- Every verb that needs root-owned paths checks writability first and, if denied,
  says: "I don't have permission to write to <dir>. Run setup again, or ask your
  engineer." (setup creates `/var/fair-ros` group-writable by group `fair-ros`).

---

## `ros2 fair setup`

One-time, per-robot, run by an engineer (jargon is allowed here, and only here).
Idempotent: re-running shows current values as defaults.

> **Run setup from a root shell that has ROS sourced and can see the robot.**
> The watchdog runs as a system service with **no** login shell, so setup
> snapshots *this shell's* ROS environment into `/etc/fair-ros/watchdog.env`
> (loaded by the unit's `EnvironmentFile=`). Becoming root with `sudo su` (or
> when `sudo -E` is blocked) strips that environment, so source ROS *inside* the
> root shell and confirm `ros2 node list` lists your robot's nodes before
> running setup:
> ```
> sudo su
> source /opt/ros/<distro>/setup.bash   # + the overlay that sets ROS_DOMAIN_ID / RMW
> ros2 node list                        # must list your robot's nodes
> ros2 fair setup
> ```

Flow:

1. **Preflight** — check running as root or with sudo-able rights; check `ros2`
   on PATH; warn (not fail) if Docker absent. **Fail (not warn)** if no ROS
   environment can be captured for the service (`ROS_DISTRO` unset) or the robot
   graph isn't visible (`ros2 node list` empty) — either means the watchdog
   would start blind and harvest an empty graph at every mission. The error
   prints the `sudo su` + `source` recipe above.
2. **Robot questions** (rich prompts, defaults from existing yaml if present):
   1. Robot name — non-empty, ≤ 40 chars.
   2. Platform (make and model) — non-empty.
   3. Serial number / asset tag — non-empty.
   4. Owning organization — non-empty.
   5. Contact email — must match `^[^@\s]+@[^@\s]+\.[^@\s]+$`.
3. **Sensor loop** — "Add a sensor? [Y/n]" repeated:
   1. Sensor id (slug, `^[a-z0-9_]+$`, unique).
   2. Type — choice list: gps / lidar / camera / imu / sonar / other.
   3. Make and model — non-empty.
   4. Topic — must start with `/`; if ROS is running, offer live topic list as
      completion candidates; accept unknown topics with a confirmation.
   5. TF frame id — optional, Enter to skip.
   6. Calibration file path — optional; if given, must exist; records a
      `calibrations[]` entry named `<sensor_id>_cal` and links it.
4. **Review panel** — full summary table, "Write this configuration? [Y/n]".
5. **Write** `/etc/fair-ros/robot_identity.yaml` (mode `0644`, dir `0755`).
6. **Install service** — create `/var/fair-ros/{spool/bags,archive}` and the
   `fair-ros` group, copy the systemd unit, `systemctl daemon-reload`,
   `systemctl enable --now fair-ros-watchdog.service`, then verify the unit is
   `active` and the state file appears within 10 s.
7. Final panel: "Setup complete. fair-ros is now watching for recordings."

Validation failures re-ask the same question with a one-line reason; three
consecutive failures on one question abort setup cleanly.

---

## `ros2 fair mission_start`

The briefing wizard. Target: under 2 minutes. Writes
`/var/fair-ros/spool/mission_context.json`.

Preflight:
- Watchdog not running → warn but continue: "Background recording assistant
  isn't running — your answers will still be saved." (dashcam principle: never
  block the human).
- Existing `mission_context.json` in spool → "There's already an unfinished
  mission from <date> by <operator>. Start a new one and replace it? [y/N]".
  No = exit 0 untouched.
- System clock not NTP-synchronised → print a non-blocking warning (the
  briefing doesn't record, so it never prompts; `mission_record` enforces it).

Questions (exactly these five, in this order):

| # | Prompt text | Field | Required | Skip |
|---|---|---|---|---|
| 1 | "What's your name?" | `identity.operator_name` | 🔴 yes | Re-asked until non-empty. Default: previous mission's operator if any. |
| 2 | "In one sentence, what is this mission trying to do?" | `intent.goal` | 🔴 yes | Re-asked until non-empty. |
| 3 | "Where are you? (place name, e.g. 'Marsh Creek, north bank')" | `intent.location_name` | 🔴 yes | Re-asked until non-empty. |
| 4 | "What's the environment like? (e.g. outdoor, indoor, marine — press Enter to skip)" | `intent.environment` | no | Enter → `null`. |
| 5 | "Anything else worth noting? (press Enter to skip)" | `intent.notes` | no | Enter → `null`. |

Then: generate `mission_id` + `created_at`, write the JSON atomically, show a
closing panel: "Mission briefing saved. Start recording with: ros2 fair
mission_record".

**ROS-env re-snapshot.** Both `mission_start` and `mission_record` write this
shell's ROS environment to `<spool>/session.env`. The recorder always has the
correct environment (it *is* the operator's live shell); the watchdog only has
the frozen `watchdog.env` snapshot, which goes blind if the operator records
under a different `ROS_DOMAIN_ID` / `RMW_IMPLEMENTATION`. At harvest time the
watchdog adopts `session.env`'s DDS discovery keys
(`ros_env.SESSION_ADOPT_KEYS`) over its own, so its harvest lands on the same
partition as the session actually recording (closes the drift that produced
empty-graph archives). `session.env` is group-writable and the watchdog runs as
root, so **only** discovery keys are adopted — never `PATH` / `LD_LIBRARY_PATH`
/ `PYTHONPATH` / overlay paths, which would be a privilege-escalation vector;
the base ROS install comes only from the root-owned `watchdog.env`. The file is
removed when the spool is cleared at `mission_close`.

---

## `ros2 fair mission_record`

Thin, safe wrapper around `ros2 bag record`.

Preflight:
- `ros2` reachable (ROS sourced) — else: "I can't find ROS 2. Make sure the
  robot software is started, then try again."
- Free space in spool ≥ 1 GiB — else warn and require explicit confirm.
- No `mission_context.json` → warn: "No mission briefing yet — recording will
  still work, and you'll be asked the briefing questions when you close the
  mission. Continue? [Y/n]".
- Watchdog not active → warn (recording still proceeds; context can be
  harvested late by the retry rule, but the warning is honest about it).
- System clock not NTP-synchronised (`utils/clock.is_synchronized()` is
  `False`) → warn and require explicit confirm. An unsynced clock stamps
  messages near the epoch, producing bags that `ros2 bag play` can't replay and
  that are useless for time-critical processing; better to catch it before
  recording than to flag the dead bag afterwards. Unknown sync state (no
  `timedatectl`) does not nag.

Subprocess (exact):

```
ros2 bag record --all
    --output /var/fair-ros/spool/bags/<mission_id or 'unbriefed'>_<YYYYMMDD-HHMMSS>
```

If `robot_identity.yaml` has an optional `recording.topics` list, that list
replaces `--all`; an optional `recording.storage` value (e.g. `mcap`) is passed
as `--storage`. Defaults: all topics, rosbag2's default storage.

Behaviour:
- rosbag2 stdout/stderr streamed to the terminal unmodified (this is the one
  place raw ROS output is acceptable — it is live diagnostics, not a message
  from fair-ros).
- Ctrl-C is forwarded as SIGINT to the child and `mission_record` waits for it
  to exit cleanly (rosbag2 needs it to write `metadata.yaml`), then prints:
  "Recording stopped. When the mission is over, run: ros2 fair mission_close".
- Child exits non-zero → "Recording stopped with a problem. The data captured
  so far is kept." Exit 1.

---

## `ros2 fair mission_close`

The single save/discard decision.

1. **Load** `harvest.json` + `mission_context.json` via `manifest/builder.py`.
   - Spool has no bags at all → "There's nothing recorded yet." Exit 1.
   - Watchdog currently RECORDING (per state file + bag dir activity) →
     "It looks like recording is still in progress. Stop it first (Ctrl-C in
     the recording window), then run this again." Exit 1.
2. **Fill gaps** — if any 🔴 required user field is missing (briefing skipped),
   ask the corresponding `mission_start` questions inline, same wording.
3. **Validate** via `manifest/validator.py`. Remaining failures print one
   plain-language line each and exit 1.
4. **Grade** the built record with `manifest/quality.assess` → `ok` /
   `degraded` / `poor`, stored in `provenance.data_quality`. `poor` means core
   content is missing (no ROS context captured, or all recordings have an
   unusable clock); `degraded` means usable with gaps (some sensors, some
   recordings).
5. **Summary panel** (rich), in order:
   - When not `ok`: a coloured **Data quality** header (INCOMPLETE / POOR) and
     its plain-language reasons; the panel border turns yellow/red.
   - **Possible duplicate** (if any): a non-blocking note when the same operator
     saved a mission at a very similar location within the last 24 h
     (`archive/duplicates.py`, fuzzy match — catches typos like
     "Crosslab"/"Crossloab"). Informational; repeat missions are legitimate.
   - Mission title line: goal, location, date.
   - Operator and robot names.
   - Recording: bag count, total duration ("42 minutes" or "length unknown"),
     total size ("3.1 GB").
   - Sensors: one line per declared sensor with ✓ or a warning glyph.
   - **Warnings**, each as its pre-rendered `plain_text` from `health_warnings`,
     plus harvest-level warnings ("I couldn't capture the software versions
     because ROS wasn't reachable", "This robot hasn't been set up yet…").
6. **Decision**:
   - Normal mission — "Save this mission? [Y/n]" (default Yes).
   - `poor` mission — the save prompt instead defaults to **No** and is worded as
     a caution ("This recording is missing important data (see above). Save it
     anyway? [y/N]"), so a near-empty recording can't be archived by reflexively
     pressing Enter.
   - **Yes** → call `archive/assembler.py` with a rich progress bar (bags can be
     gigabytes). On success: "Mission saved: <archive dir name>". Spool is now
     empty. Exit 0.
   - **No** → "Throw away this recording and all its data? [y/N]"
     - Yes → delete spool contents, "Recording discarded." Exit 0.
     - No → "Nothing was changed — the recording is still in the spool." Exit 0
       (the operator can rerun mission_close later).
7. Assembly failure → spool left intact, plain-language error naming the cause
   (disk full, permissions), exit 1. Never half-archived (see `specs/archive.md`).

`data_quality` is also written to the SQLite index, so `ros2 fair list` flags
degraded/poor missions and `--json` exposes the field.

Warning-generation logic lives in `topic_health.py` (per-bag) and `builder.py`
(harvest-level); the quality verdict in `manifest/quality.py`; `ui/review.py`
only renders pre-built strings.

---

## `ros2 fair mission_status`

Read-only, instant, no prompts. Renders one panel from `watchdog.state`,
spool contents, and `mission_context.json`:

- Assistant (watchdog): "watching" / "recording (started 14:03, 12 minutes
  ago)" / "wrapping up" / "not running" (PID dead or state file stale > 5 min
  heartbeat while RECORDING).
- Mission briefing: operator + goal, or "not started yet".
- Recording: active bag, size so far, "growing" indicator; or "none".
- Context captured: per harvest module, plain words ("software versions ✓",
  "robot description ✗ — will retry"). Modules: robot identity, computer
  details, Python environment, connected hardware, software versions and
  settings, robot description, container software. Glyphs: `✓` ok, `⚠` partial,
  `–` skipped ("not used on this robot"), `✗` failed/timeout ("will keep
  trying"). The `partial` state (`⚠`) applies to the Python environment and
  connected-hardware modules when some sources were unavailable.

`--json` flag emits the raw machine-readable status for scripts (the one
sanctioned JSON output, since its audience is scripts, not operators).

---

## `ros2 fair list`

Queries the SQLite index only (never scans the archive directory).

Default output: rich table, newest first.

| Column | Source |
|---|---|
| Date | `created_at`, local time, `YYYY-MM-DD HH:MM` |
| Mission | `goal`, truncated 40 chars |
| Location | `location` |
| Operator | `operator` |
| Duration | humanised from `duration_s` |
| Size | humanised from `size_bytes` |
| ⚠ | `warning_count` (blank when 0) |
| Data | `data_quality`: blank for `ok`/unset, `partial` (yellow) for `degraded`, `poor` (red) |

Options:
- `--operator <text>`, `--location <text>` — case-insensitive substring filters.
- `--since <YYYY-MM-DD>`, `--until <YYYY-MM-DD>`.
- `--quality {ok,degraded,poor}` — only missions with that data-quality verdict
  (e.g. `--quality poor` to find the ones that need attention).
- `--limit <n>` — default 20; footer line "Showing 20 of 134 missions" when
  truncated.
- `--path` — adds the archive path column (for engineers copying data off).
- `--json` — emits `{"missions": [...], "total": <n>, "shown": <n>}` to stdout,
  one object per index row (all columns). Sanctioned JSON output for scripts;
  bypasses the table and the plain-language empty/no-index messages (those
  become an empty `missions` list).

Empty result: "No missions found." Index file missing: "No missions have been
saved on this robot yet."

---

## `ros2 fair diff [<mission_a>] [<mission_b>]`

Compares two saved missions and shows only what changed, section by section
(mission context, software, sensors, ROS graph, recordings). Sections with no
differences are omitted.

Each argument identifies a mission by **number** (`1` = newest, `2` =
second-newest, …), **archive path**, or **mission ID**. With no arguments it
compares the two most recent missions (`A` = older, `B` = newer); supplying a
single argument is an error.

Options:
- `--json` — emits `{"mission_a": {…}, "mission_b": {…}, "changes": {<section>:
  [{"label", "a", "b"}, …]}}` to stdout. `changes` contains only sections that
  differ; `a`/`b` are the before/after values as shown in the table (empty `a` =
  added in B, empty `b` = removed in B).

Index file missing, an out-of-range number, or an unresolvable identifier each
produce a plain-language error and exit 1.

## `ros2 fair verify [<mission>]`

Re-checks that a saved mission archive is complete and unmodified — the question
a data consumer has months later. Read-only: it never touches the archive or the
index. The argument identifies a mission the same way `diff` does (number,
archive path, or mission ID); with no argument it verifies the most recent
mission.

Runs these checks and renders each as a plain-language ✓/!/✗ line in a single
panel:

| Check | Status on problem |
|---|---|
| `mission_record.json` loads and validates against the schema | ✗ fail (stops here) |
| `ro-crate-metadata.json` is valid JSON-LD (deep-loaded with `rocrate` if installed) | ✗ fail (! if `rocrate` absent — JSON-only) |
| `README.md`, `harvest/harvest.json` present | ! warn |
| each bag file still matches its per-file `sha256` recorded at archive time (pre-1.0 archives without checksums fall back to a structural check — metadata + listed storage files present — reported `!`) | ✗ fail |
| each calibration file still matches the `sha256` recorded at archive time | ✗ fail |
| every `File` entity referenced by the crate exists on disk | ✗ fail |
| the mission is registered in the SQLite index at this path | ! warn (`reindex()` can fix) |

Overall result: **PASS** (all ✓), **PASS (with notes)** (some ! but no ✗), or
**FAIL** (any ✗). Exit code is `0` unless any check failed, then `1`.

Options:
- `--json` — emits `{"archive": <path>, "result": "ok|warn|fail", "checks":
  [{"status", "title", "detail"}, …]}` to stdout.

Bag bytes are pinned at archive time: the assembler records a sha256 for every
file in each bag (`Bag.file_sha256`), so verify detects byte-level modification,
not just missing files. Archives written before 1.0 have no bag checksums and
fall back to the structural check (reported with a `!`).

---

## `ros2 fair doctor`

Preflight readiness self-check, run before a mission. Where `verify` asks "is
this *saved archive* still intact?", `doctor` asks "is this *robot* ready to
capture a good mission *right now*?" — catching the failure modes that otherwise
only surface as an empty or unusable archive afterwards. Read-only; takes no
mission argument.

Runs these checks, each a plain-language ✓/!/✗/– line (with a `→ hint` under any
✗/!) in one panel:

| Check | Status on problem |
|---|---|
| robot identity file present and valid (`harvest/robot_identity`) | ✗ fail |
| recording assistant (watchdog) running, heartbeat fresh | ✗ fail (! if stale) |
| ROS 2 reachable from **this shell** (`ros2 node list` non-empty) | ✗ fail (! if reachable but no nodes) |
| ROS environment sourced here (`ROS_DISTRO` set; reports rmw/domain) | ! warn |
| the **service's** env (`/etc/fair-ros/watchdog.env`) exists with `ROS_DISTRO`, and its `ROS_DOMAIN_ID` / `RMW_IMPLEMENTATION` match this shell | ✗ fail if missing/no distro; ! warn on domain/RMW drift |
| the **watchdog's own** last graph harvest succeeded (service-context truth — the empty-archive failure) | ✗ fail (– if it hasn't harvested yet) |
| system clock NTP-synchronised (`utils/clock`) | ✗ fail (– if undeterminable) |
| `mcap` available for bag timing/health | ! warn |
| spool free space ≥ 1 GiB | ✗ fail |
| Docker reachable | – skip (optional) |

The service-harvest check is the one that distinguishes "ROS works in my shell"
from "the background service can actually see ROS" — the exact gap that produced
empty archives on the real robot.

Overall result: **READY** (no ✗/!), **READY (with warnings)** (some ! but no ✗),
or **NOT READY** (any ✗). A failing check (`✗`) makes the exit code `1`; warnings
and skips do not. Each check that raises unexpectedly is itself reported as a ✗
rather than crashing the command.

Options:
- `--json` — emits `{"result": "ok|warn|fail", "checks": [{"status", "title",
  "detail", "hint"}, …]}` to stdout.

---

## `ros2 fair export [<mission>]`

Packages a saved mission archive (an RO-Crate *directory*) into a single
portable file for sharing or deposit — replacing the operator's manual hand-zip.
The mission argument is resolved the same way as `diff`/`verify` (number, archive
path, or mission ID); no argument exports the most recent mission. Read-only with
respect to the archive and index.

Behaviour:
- Bundles the whole crate under a top-level folder named after the archive (so
  unpacking yields `<name>/ro-crate-metadata.json`, …).
- Default format `zip`; `--format tar` writes a `.tar`. Both are **stored
  uncompressed** — bag data (MCAP, images) is already compressed, so deflating
  multi-GB recordings only costs CPU. ZIP64 is enabled for large bags.
- Written atomically (`<dest>.part` then renamed); on any error the partial file
  is removed and nothing is left behind.
- Refuses to overwrite an existing output unless `--force`.
- Writes a `sha256sum`-compatible sidecar `<dest>.sha256` (`"<sha256>  <name>"`)
  so the recipient can prove the transfer with `sha256sum -c`. Internal
  per-file integrity is still checkable with `ros2 fair verify` after unpacking.
- Runs `verify` on the source first; if it fails, prints a warning but still
  exports (the operator asked, and a flawed copy can be worth shipping).
- Shows a transient byte-progress bar while packaging.

Output location (`--output`/`-o`): a file path is used verbatim; a directory (or
a trailing `/`) writes `<name>.<ext>` into it; default is the current directory.

Exit code `0` on success, `1` on a resolve/verify-load error, an existing output
without `--force`, or a write failure.

Options:
- `--output`, `-o` — output file or directory.
- `--format {zip,tar}` — bundle format (default `zip`).
- `--force` — overwrite an existing output file.
- `--json` — emits `{"mission_id", "source", "bundle", "format", "size_bytes",
  "sha256", "checksum_file", "verify_result"}` to stdout (`verify_result` is
  `ok|warn|fail|unknown`; `unknown` if the integrity check couldn't run).

---

## `ros2 fair repair [<mission>]`

Makes a saved mission's **unplayable** recordings playable again. A bag recorded
with an unsynchronised clock has most messages stamped near the epoch (1970), so
`ros2 bag play` honours the resulting ~56-year timeline and stalls. This writes a
re-stamped, immediately-playable **copy** of each affected recording — the
original archive is never touched, so its `file_sha256` checksums and `verify`
result still hold.

The argument is a mission (number / archive path / mission ID, like `verify`) or
a path to a single bag directory; no argument repairs the most recent mission.

Behaviour:
- For each recording, decides via `utils/bag_repair.needs_repair` (re-derived
  from the message timestamps) whether the clock is unrecoverable. Healthy
  recordings are left alone and reported as "already playable"; `--all`
  re-stamps every recording regardless.
- Writes each repaired recording as `<output>/<bag-name>/` containing a
  re-stamped MCAP **and a regenerated `metadata.yaml`** (topic types/QoS reused
  from the source, only timing and the storage file fixed), so the result plays
  directly — no `ros2 bag reindex` needed.
- Output goes to `--output`/`-o` (default `./<name>_repaired/`); refuses a
  non-empty output directory unless `--force`.
- Only MCAP bags can be repaired; other formats are reported as skipped.

**The repaired timing is synthetic** (messages keep their original order, types
and bytes; inter-message spacing is spread evenly over `--duration`, default the
span of the few real stamps else 60 s). Good for inspection and playback; not
for time-critical processing. The only real fix is to sync the clock before
recording — see `docs/recovering-bad-clock-bags.md`. Exit code `0` (including
when nothing needed repair), `1` only on a bad target.

Options:
- `--output`, `-o` — directory for repaired recordings.
- `--all` — re-stamp every recording, not only the bad-clock ones.
- `--duration SECONDS` — target playback length per repaired recording.
- `--force` — write into a non-empty output directory.
- `--json` — emits `{"target", "output", "repaired", "bags": [{"bag", "status",
  …}]}` to stdout.
