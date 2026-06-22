# Live ROS smoke test

The automated suite runs with **no ROS** (subprocess and rclpy calls are mocked
or driven by synthetic fixtures), which keeps CI fast and portable but cannot
prove the pieces that only exist on a real robot:

- that `ros2 fair` is actually discovered by `ros2cli` (entry-point registration);
- that the subprocess ROS-graph harvest reads a real running graph;
- that the rclpy `/robot_description` capture works against a latched publisher;
- that the full **record → harvest → archive → verify** pipeline runs against a
  real `ros2 bag record` output (Jazzy's default MCAP storage).

`tests/integration/test_ros_smoke.py` covers exactly these. The tests are marked
`@pytest.mark.ros` and **deselected by default** (`pyproject.toml`
`addopts = -m "not ros"`), so a normal `pytest` run and CI never touch them.

## Prerequisites

- A sourced ROS 2 environment (`ros2` on `PATH`, `$ROS_DISTRO` set). Tested
  against **Jazzy**.
- `demo_nodes_cpp` (provides the `talker` node) — used by the graph and
  lifecycle tests; they `skip` if it is absent. On Debian/Ubuntu:
  `sudo apt install ros-$ROS_DISTRO-demo-nodes-cpp`.
- The package installed into that environment with its test extras:
  `pip install -e '.[test]'` (brings in `pytest`, `rocrate`, `mcap`).

## Running

```bash
source /opt/ros/$ROS_DISTRO/setup.bash
pip install -e '.[test]'
pytest -m ros -v
```

`-m ros` overrides the default deselection. On a box **without** ROS sourced the
same command skips every test with a clear reason rather than failing, so it is
safe to run anywhere.

## What each test does

| Test | Validates | Notes |
|---|---|---|
| `test_fair_verb_is_discoverable` | `ros2 fair --help` lists the verbs | pure entry-point check; no extra nodes |
| `test_ros_graph_harvest_sees_live_node` | `ros_graph.harvest()` sees `talker` + `/chatter` | starts/stops a `talker` |
| `test_ros_descriptions_captures_latched_urdf` | rclpy harvest reads a latched `/robot_description` | publishes a tiny URDF itself |
| `test_full_record_harvest_archive_verify` | real bag → real harvest → crate → `verify` passes | records ~5 s of `/chatter`; the most environment-sensitive |

## Safety / side effects

The tests are read-mostly but **do** spawn ROS processes (`talker`,
`ros2 bag record`) and write a small temporary mission archive under a
`tmp_path`-backed spool — never the real `/var/fair-ros`. They do **not** run
`ros2 fair setup` (which needs root and installs a systemd unit); the lifecycle
test writes a temporary `robot_identity.yaml` directly instead. Nothing touches
the machine's real configuration.

## Not covered here

The watchdog's inotify state machine and restart recovery are exercised in full
by the unit tests (`tests/unit/test_watchdog.py`) with a fake inotify injector,
so they are not repeated against live inotify here.
