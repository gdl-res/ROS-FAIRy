import json

import pytest
from inotify_simple import Event, flags

from fair_ros.manifest import builder
from fair_ros.utils import paths
from fair_ros.watchdog import watchdog as wd_mod
from fair_ros.watchdog.watchdog import IDLE, RECORDING, Watchdog
from tests.conftest import make_bag

T0 = 1_750_000_000.0


class FakeINotify:
    """Mock inotify event injector (specs testing strategy)."""

    def __init__(self):
        self._next_wd = 1
        self.watches: dict[str, int] = {}
        self.queue: list[Event] = []

    def add_watch(self, path, mask):
        wd = self._next_wd
        self._next_wd += 1
        self.watches[str(path)] = wd
        return wd

    def rm_watch(self, wd):
        for path, known in list(self.watches.items()):
            if known == wd:
                del self.watches[path]

    def read(self, timeout=None):
        events, self.queue = self.queue, []
        return events

    # -- injection helpers -------------------------------------------------
    def emit(self, path, mask, name):
        self.queue.append(Event(self.watches[str(path)], mask, 0, name))

    def emit_dir_created(self, bags_dir, bag_name):
        self.emit(bags_dir, flags.CREATE | flags.ISDIR, bag_name)

    def emit_file(self, bag_dir, name, mask=flags.CREATE):
        self.emit(bag_dir, mask, name)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


GOOD_STATUS = {"robot_identity": "ok", "system_info": "ok", "ros_graph": "ok",
               "ros_descriptions": "ok", "docker_info": "skipped"}
IDENTITY = {
    "robot": {"name": "Heron-02", "platform": "Heron", "serial_number": "H02",
              "owner_organization": "Lab", "owner_contact": "a@b.c"},
    "sensors": [{"sensor_id": "gps0", "type": "gps", "make_model": "F9P",
                 "topic": "/fix", "frame_id": None, "calibration_ref": None}],
    "calibrations": [], "default_license": None,
}


def good_pipeline():
    return builder.compose_harvest(
        identity=IDENTITY,
        system={"hostname": "r1", "kernel": "Linux 6.8", "arch": "x86_64",
                "ros_distro": "jazzy", "apt_ros_versions": {}},
        graph={"captured_at": "2026-06-12T14:03:00+00:00", "nodes": [],
               "topics": [{"name": "/fix", "type": "sensor_msgs/msg/NavSatFix"}],
               "ros_packages": [], "parameters": {}, "complete": True},
        docker=None, descriptions={"robot_description": "<robot/>",
                                   "tf_static": []},
        harvest_status=GOOD_STATUS)


@pytest.fixture
def rig(fair_dirs):
    ino = FakeINotify()
    clock = FakeClock()
    dog = Watchdog(inotify=ino, clock=clock, pipeline=good_pipeline,
                   harvest_in_thread=False)
    dog.start()
    return ino, clock, dog


def _steady(start, end, hz):
    n = int((end - start) * hz)
    return [start + i / hz for i in range(n + 1)]


def _record_bag(rig, name="rosbag2_test", with_metadata=True):
    ino, clock, dog = rig
    bag = make_bag(paths.bags_dir() / name,
                   {"/fix": _steady(T0, T0 + 60, 10)})
    if not with_metadata:
        (bag / "metadata.yaml").unlink()
    ino.emit_dir_created(paths.bags_dir(), name)
    dog.step(0)
    ino.emit_file(bag, f"{name}_0.db3")
    dog.step(0)
    return bag


def test_full_cycle(rig):
    ino, clock, dog = rig
    assert dog.state == IDLE
    bag = _record_bag(rig)

    assert dog.state == RECORDING
    harvest, _ = builder.load_spool()
    assert harvest["robot"]["name"] == "Heron-02"
    assert harvest["sensors"][0]["detected_at_start"] is True
    assert harvest["bags"] == []
    state = json.loads(paths.watchdog_state_path().read_text())
    assert state["state"] == "RECORDING"
    assert state["active_bag_dir"] == str(bag)

    ino.emit_file(bag, "metadata.yaml", flags.CLOSE_WRITE)
    dog.step(0)

    assert dog.state == IDLE
    harvest, _ = builder.load_spool()
    assert len(harvest["bags"]) == 1
    rec = harvest["bags"][0]
    assert rec["storage_format"] == "sqlite3"
    assert rec["message_count"] == 601
    assert rec["topics"][0]["name"] == "/fix"
    assert rec["health_warnings"] == []
    assert harvest["provenance"]["harvested_at"]
    assert json.loads(paths.watchdog_state_path().read_text())["state"] == "IDLE"


def test_storage_present_before_watch_still_records(rig):
    """Race: the first storage chunk lands before W2 is armed.

    inotify never delivers a CREATE for a file that predates ``add_watch``,
    so a bag whose first ``.db3`` appears in the window between the directory
    showing up and the per-bag watch being armed would be missed entirely —
    no RECORDING, no context harvest. Here only the dir-created event is
    emitted; the storage file already exists and no file event ever fires.
    The watchdog must scan the new directory on arm and still record.
    """
    ino, clock, dog = rig
    name = "rosbag2_race"
    bag = make_bag(paths.bags_dir() / name, {"/fix": _steady(T0, T0 + 60, 10)})
    (bag / "metadata.yaml").unlink()  # recording still in progress

    ino.emit_dir_created(paths.bags_dir(), name)
    dog.step(0)  # no storage-file event is ever emitted

    assert dog.state == RECORDING
    assert dog.active_bag_dir == bag
    harvest, _ = builder.load_spool()
    assert harvest["robot"]["name"] == "Heron-02"
    state = json.loads(paths.watchdog_state_path().read_text())
    assert state["state"] == "RECORDING"
    assert state["active_bag_dir"] == str(bag)


def test_inactivity_finalises_crashed_bag(rig):
    ino, clock, dog = rig
    _record_bag(rig, with_metadata=False)
    assert dog.state == RECORDING

    clock.now += wd_mod.BAG_INACTIVITY_S + 1
    dog.step(0)

    assert dog.state == IDLE
    harvest, _ = builder.load_spool()
    rec = harvest["bags"][0]
    assert rec["storage_format"] == "unknown"
    assert any("unexpectedly" in w["plain_text"]
               for w in rec["health_warnings"])


def test_activity_defers_inactivity_timeout(rig):
    ino, clock, dog = rig
    bag = _record_bag(rig)
    clock.now += wd_mod.BAG_INACTIVITY_S - 5
    ino.emit_file(bag, f"{bag.name}_0.db3", flags.MODIFY)
    dog.step(0)
    clock.now += wd_mod.BAG_INACTIVITY_S - 5
    dog.step(0)
    assert dog.state == RECORDING


def test_ros_down_schedules_retry_and_recovers(fair_dirs):
    ino, clock = FakeINotify(), FakeClock()
    calls = {"n": 0}

    def flaky_pipeline():
        calls["n"] += 1
        if calls["n"] == 1:
            return builder.compose_harvest(
                identity=IDENTITY, system=None, graph=None, docker=None,
                descriptions=None,
                harvest_status={**GOOD_STATUS, "ros_graph": "failed",
                                "ros_descriptions": "failed"})
        return good_pipeline()

    dog = Watchdog(inotify=ino, clock=clock, pipeline=flaky_pipeline,
                   harvest_in_thread=False)
    dog.start()
    bag = _record_bag((ino, clock, dog))
    harvest, _ = builder.load_spool()
    assert harvest["provenance"]["harvest_status"]["ros_graph"] == "failed"
    assert dog._next_retry is not None

    # rosbag2 keeps writing while time passes, so the bag stays active
    for _ in range(4):
        clock.now += wd_mod.ROS_RETRY_INTERVAL_S / 4 + 1
        ino.emit_file(bag, f"{bag.name}_0.db3", flags.MODIFY)
        dog.step(0)
    assert dog.state == RECORDING
    harvest, _ = builder.load_spool()
    assert harvest["provenance"]["harvest_status"]["ros_graph"] == "ok"
    assert calls["n"] == 2
    assert dog._next_retry is None


def test_recovery_finalises_leftover_bag(fair_dirs):
    bag = make_bag(paths.bags_dir() / "rosbag2_left",
                   {"/fix": _steady(T0, T0 + 60, 10)})
    ino, clock = FakeINotify(), FakeClock()
    dog = Watchdog(inotify=ino, clock=clock, pipeline=good_pipeline,
                   harvest_in_thread=False)
    dog.start()
    assert dog.state == IDLE
    harvest, _ = builder.load_spool()
    assert len(harvest["bags"]) == 1
    assert harvest["bags"][0]["path"] == str(bag)


def test_unreliable_clock_bag_records_unknown_duration(fair_dirs):
    """A bag whose clock was broken for most of the run is finalised with an
    unknown duration and no fabricated start/end times or data rates."""
    bad = [float(i) for i in range(1, 31)]      # 30 near-epoch stamps
    good = _steady(T0, T0 + 5, 2)               # 11 real stamps
    bag = make_bag(paths.bags_dir() / "rosbag2_badclock",
                   {"/data": bad + good})

    wd_mod.append_bag_record(bag)

    harvest, _ = builder.load_spool()
    rec = harvest["bags"][0]
    assert rec["duration_s"] is None
    assert rec["start_time"] is None and rec["end_time"] is None
    assert all(t["avg_frequency_hz"] is None for t in rec["topics"])
    assert any(w["kind"] == "unreliable_clock" for w in rec["health_warnings"])
    # the record still validates against the schema (timing fields optional).
    from fair_ros.manifest.schema import Bag
    Bag.model_validate(rec)


def test_recovery_resumes_recording(fair_dirs):
    bag = make_bag(paths.bags_dir() / "rosbag2_live",
                   {"/fix": _steady(T0, T0 + 60, 10)})
    (bag / "metadata.yaml").unlink()
    ino, clock = FakeINotify(), FakeClock()
    dog = Watchdog(inotify=ino, clock=clock, pipeline=good_pipeline,
                   harvest_in_thread=False)
    dog.start()
    assert dog.state == RECORDING
    assert dog.active_bag_dir == bag


def test_second_bag_queued_then_recorded(rig):
    ino, clock, dog = rig
    bag_a = _record_bag(rig, "rosbag2_a")
    bag_b = make_bag(paths.bags_dir() / "rosbag2_b",
                     {"/fix": _steady(T0 + 100, T0 + 160, 10)})
    ino.emit_dir_created(paths.bags_dir(), "rosbag2_b")
    dog.step(0)
    ino.emit_file(bag_b, "rosbag2_b_0.db3")
    dog.step(0)
    assert dog.active_bag_dir == bag_a
    assert dog.queued_bags == [bag_b]

    ino.emit_file(bag_a, "metadata.yaml", flags.CLOSE_WRITE)
    dog.step(0)
    assert dog.state == RECORDING
    assert dog.active_bag_dir == bag_b

    ino.emit_file(bag_b, "metadata.yaml", flags.CLOSE_WRITE)
    dog.step(0)
    harvest, _ = builder.load_spool()
    assert [b["path"] for b in harvest["bags"]] == [str(bag_a), str(bag_b)]


def test_harvest_rewrite_preserves_bags(rig):
    ino, clock, dog = rig
    bag = _record_bag(rig)
    ino.emit_file(bag, "metadata.yaml", flags.CLOSE_WRITE)
    dog.step(0)
    dog._harvest_once()  # e.g. a retry firing after finalise
    harvest, _ = builder.load_spool()
    assert len(harvest["bags"]) == 1


def test_read_state(fair_dirs):
    assert wd_mod.read_state() is None
    paths.watchdog_state_path().write_text('{"state": "IDLE"}')
    assert wd_mod.read_state() == {"state": "IDLE"}
    paths.watchdog_state_path().write_text("not json")
    assert wd_mod.read_state() is None
