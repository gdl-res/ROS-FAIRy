"""Opt-in smoke tests against a live, sourced ROS 2 environment.

These validate the parts mocked tests cannot:
  - the `ros2 fair` verb is actually discovered by ros2cli (entry_points);
  - the subprocess-based ROS graph harvest sees a real running node;
  - the rclpy `/robot_description` capture reads a latched publisher;
  - the full record -> harvest -> archive -> verify pipeline runs against a
    real `ros2 bag record` output (Jazzy's default MCAP storage).

They are DESELECTED by default (pyproject `addopts = -m "not ros"`). Run them on
a robot / dev box with ROS sourced:

    pip install -e '.[test]'
    source /opt/ros/<distro>/setup.bash
    pytest -m ros -v

See docs/real-robot-smoke-test.md. The graph and lifecycle tests need
`demo_nodes_cpp` (talker) and skip if it is not installed.
"""

import signal
import subprocess
import time
from contextlib import contextmanager

import pytest

from fair_ros.archive import assembler
from fair_ros.harvest import ros_descriptions, ros_graph
from fair_ros.manifest import builder
from fair_ros.subcommands import verify
from fair_ros.utils import fsio, paths
from fair_ros.watchdog import watchdog

pytestmark = pytest.mark.ros


@contextmanager
def _background(cmd: list[str]):
    """Run a ROS process in the background; stop it cleanly with SIGINT.

    rosbag2 needs SIGINT (not SIGTERM) to flush metadata.yaml on stop, so all
    background processes are stopped that way.
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        yield proc
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _wait_for_node(substr: str, timeout: float = 15) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if any(substr in n for n in ros_graph.list_nodes()):
                return True
        except ros_graph.RosGraphError:
            pass
        time.sleep(0.5)
    return False


@pytest.fixture
def talker():
    """A running demo talker, or skip if demo_nodes_cpp isn't installed."""
    try:
        with _background(["ros2", "run", "demo_nodes_cpp", "talker"]) as proc:
            if not _wait_for_node("talker"):
                pytest.skip("demo_nodes_cpp talker did not come up "
                            "(package not installed?)")
            yield proc
    except FileNotFoundError:
        pytest.skip("ros2 not on PATH")


def test_fair_verb_is_discoverable():
    """ros2cli must find the `fair` command and its verbs (entry_points)."""
    out = subprocess.run(["ros2", "fair", "--help"],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    text = out.stdout + out.stderr
    for verb in ("mission_start", "mission_close", "list", "diff", "verify"):
        assert verb in text, f"verb '{verb}' missing from `ros2 fair --help`"


def test_ros_graph_harvest_sees_live_node(talker):
    graph = ros_graph.harvest()
    assert any("talker" in n for n in graph["nodes"]), graph["nodes"]
    chatter = next((t for t in graph["topics"] if t["name"] == "/chatter"),
                   None)
    assert chatter is not None, "no /chatter topic in the live graph"
    assert "String" in chatter["type"]
    assert graph["ros_packages"], "expected a non-empty package list"


def test_ros_descriptions_captures_latched_urdf():
    """Publish a latched /robot_description; confirm the rclpy harvest reads it."""
    rclpy = pytest.importorskip("rclpy")
    from rclpy.qos import (
        DurabilityPolicy,
        HistoryPolicy,
        QoSProfile,
        ReliabilityPolicy,
    )
    from std_msgs.msg import String

    urdf = "<robot name='smoke'><link name='base'/></robot>"
    latched = QoSProfile(depth=1, history=HistoryPolicy.KEEP_LAST,
                         reliability=ReliabilityPolicy.RELIABLE,
                         durability=DurabilityPolicy.TRANSIENT_LOCAL)

    rclpy.init()
    try:
        node = rclpy.create_node("fair_ros_smoke_urdf_pub")
        pub = node.create_publisher(String, "/robot_description", latched)
        pub.publish(String(data=urdf))
        # let the latched sample go out on the wire
        end = time.monotonic() + 1.0
        while time.monotonic() < end:
            rclpy.spin_once(node, timeout_sec=0.1)
        # harvest uses its own private rclpy context and a late-joining sub
        result = ros_descriptions.harvest(timeout_s=5)
        captured = result["robot_description"]
        # On a bare graph our latched publisher is the only source, so we read
        # it back verbatim. On a real robot a transient-local
        # /robot_description publisher already exists and the late-joining sub
        # may latch *that* sample instead — which is fine; the point of this
        # smoke test is that the latched-read path yields a well-formed URDF.
        assert captured is not None, "harvest captured no /robot_description"
        assert captured == urdf or ("<robot" in captured
                                    and "</robot>" in captured)
    finally:
        rclpy.shutdown()


_IDENTITY_YAML = """\
robot:
  name: SmokeBot
  platform: Test Rig
  serial_number: SMOKE-1
owner:
  organization: Lab
  contact_email: smoke@example.org
sensors:
  - sensor_id: chat0
    type: other
    make_model: Demo Talker
    topic: /chatter
"""


def test_full_record_harvest_archive_verify(talker, fair_dirs):
    """End-to-end on real ROS: record a bag, run the real harvest pipeline,
    assemble the crate, and verify it."""
    (fair_dirs["cfg"] / "robot_identity.yaml").write_text(_IDENTITY_YAML)

    bag_dir = paths.bags_dir() / "smoke_bag"
    with _background(["ros2", "bag", "record", "-o", str(bag_dir), "/chatter"]):
        time.sleep(5)  # capture a few seconds of /chatter
    assert (bag_dir / "metadata.yaml").is_file(), "rosbag2 wrote no metadata"

    # Real harvest pipeline (ros_graph, system, python_env, hardware, docker,
    # descriptions) + finalise the real bag, exactly as the watchdog would.
    fsio.atomic_write_json(paths.harvest_json_path(), watchdog.run_pipeline())
    watchdog.append_bag_record(bag_dir)

    harvest, _ = builder.load_spool()
    assert harvest["robot"]["name"] == "SmokeBot"
    assert any("talker" in n for n in harvest["ros_graph"]["nodes"])

    context = builder.new_mission_context(
        operator_name="Smoke Tester", goal="Live ROS smoke test",
        location_name="CI Lab")
    fsio.atomic_write_json(paths.mission_context_path(), context)

    record = builder.build(harvest, context)
    crate = assembler.assemble(record, harvest)

    checks = verify.verify_archive(crate)
    failures = [c for c in checks if c["status"] == verify.FAIL]
    assert not failures, failures
    # the bag came from real `ros2 bag record`, so it carries per-file hashes
    assert record.bags[0].file_sha256
