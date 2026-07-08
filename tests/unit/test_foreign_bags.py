"""Foreign-bag detection: /proc recorder scan, watchdog adoption, `adopt`,
assembler copy-not-move, and the vanished-source warning (specs/watchdog.md,
specs/cli.md, specs/archive.md)."""

import os
import shutil
from types import SimpleNamespace
from unittest import mock

from inotify_simple import flags

from fair_ros.archive import assembler
from fair_ros.manifest import builder
from fair_ros.subcommands import adopt
from fair_ros.utils import fsio, paths
from fair_ros.watchdog import recorder_scan
from fair_ros.watchdog import watchdog as wd_mod
from fair_ros.watchdog.watchdog import IDLE, RECORDING, Watchdog
from tests.conftest import make_bag
from tests.unit.test_watchdog import (
    T0,
    FakeClock,
    FakeINotify,
    _steady,
    good_pipeline,
)

SCAN_S = wd_mod.FOREIGN_SCAN_INTERVAL_S


# -- recorder_scan parsing helpers (pure) --------------------------------------

def test_is_record_cmd_matches_only_recorder():
    assert recorder_scan._is_record_cmd(["ros2", "bag", "record", "-o", "x"])
    assert recorder_scan._is_record_cmd(
        ["python3", "/opt/ros/jazzy/bin/ros2", "bag", "record", "--all"])
    assert not recorder_scan._is_record_cmd(["ros2", "bag", "play", "x"])
    assert not recorder_scan._is_record_cmd(["ros2", "bag", "info", "x"])
    assert not recorder_scan._is_record_cmd(["ros2", "bag", "convert", "x"])
    assert not recorder_scan._is_record_cmd(["ros2", "topic", "list"])
    assert not recorder_scan._is_record_cmd([])
    # an output dir or topic named like another verb must not be excluded
    assert recorder_scan._is_record_cmd(
        ["ros2", "bag", "record", "-o", "info", "/chatter"])


def test_output_arg_forms():
    assert recorder_scan._output_arg(["record", "-o", "run"]) == "run"
    assert recorder_scan._output_arg(["record", "--output", "run"]) == "run"
    assert recorder_scan._output_arg(["record", "--output=run"]) == "run"
    assert recorder_scan._output_arg(["record", "-o=run"]) == "run"
    assert recorder_scan._output_arg(["record", "--all"]) is None


def test_resolve_output_explicit(tmp_path):
    active = make_bag(tmp_path / "run", {"/t": [1.0, 2.0]})
    (active / "metadata.yaml").unlink()  # storage present, no metadata = live
    argv = ["ros2", "bag", "record", "-o", str(active), "/t"]
    assert recorder_scan._resolve_output(argv, tmp_path) == active

    finished = make_bag(tmp_path / "done", {"/t": [1.0]})  # has metadata
    argv2 = ["ros2", "bag", "record", "-o", "done", "/t"]
    assert recorder_scan._resolve_output(argv2, tmp_path) is None
    assert finished.is_dir()


def test_resolve_output_default_name(tmp_path):
    bag = make_bag(tmp_path / "rosbag2_2026_06_24-10_00_00", {"/t": [1.0]})
    (bag / "metadata.yaml").unlink()
    assert recorder_scan._resolve_output(["ros2", "bag", "record"], tmp_path) == bag


def test_scan_returns_empty_when_no_recorder():
    assert recorder_scan.scan() == []  # no ros2 bag record on this machine


# -- recorder_scan against a fake /proc tree ------------------------------------

RECORD_ARGV = ["python3", "/opt/ros/jazzy/bin/ros2", "bag", "record"]


def _fake_proc(tmp_path, monkeypatch, pid, argv, cwd=None, fd_targets=(),
               environ=b""):
    """Build a minimal /proc/<pid>/ and point recorder_scan at it."""
    p = tmp_path / "proc" / str(pid)
    (p / "fd").mkdir(parents=True)
    (p / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (p / "environ").write_bytes(environ)
    if cwd is not None:
        os.symlink(cwd, p / "cwd")
    for i, target in enumerate(fd_targets, start=3):
        os.symlink(target, p / "fd" / str(i))
    monkeypatch.setattr(recorder_scan, "PROC", tmp_path / "proc")
    monkeypatch.setattr(recorder_scan, "_reported_unresolved", set())


def test_fd_output_finds_open_storage_file(tmp_path, monkeypatch):
    bag = tmp_path / "somewhere" / "run42"
    bag.mkdir(parents=True)
    (bag / "run42_0.mcap").touch()
    _fake_proc(tmp_path, monkeypatch, 100, RECORD_ARGV,
               fd_targets=["pipe:[1]", "socket:[2]", str(bag / "run42_0.mcap")])
    assert recorder_scan._fd_output("100") == bag


def test_fd_output_ignores_deleted_and_nonstorage(tmp_path, monkeypatch):
    _fake_proc(tmp_path, monkeypatch, 101, RECORD_ARGV,
               fd_targets=["/gone/run_0.mcap (deleted)", "anon_inode:[ev]",
                           "/no/such/dir/run_0.db3"])  # parent dir missing
    assert recorder_scan._fd_output("101") is None


def test_scan_resolves_via_fd_without_output_arg(tmp_path, monkeypatch):
    # No -o and a cwd with no rosbag2_* dir: only the open fd can answer.
    bag = tmp_path / "elsewhere" / "mybag"
    bag.mkdir(parents=True)
    (bag / "mybag_0.db3").touch()
    cwd = tmp_path / "home"
    cwd.mkdir()
    _fake_proc(tmp_path, monkeypatch, 200, RECORD_ARGV + ["/scan"], cwd=cwd,
               fd_targets=[str(bag / "mybag_0.db3")],
               environ=b"ROS_DOMAIN_ID=7\0SECRET=x\0")
    found = recorder_scan.scan()
    assert [r["pid"] for r in found] == [200]
    assert found[0]["output_dir"] == bag.resolve()
    assert found[0]["discovery"] == {"ROS_DOMAIN_ID": "7"}


def test_scan_falls_back_to_argv_cwd_when_no_fd(tmp_path, monkeypatch):
    cwd = tmp_path / "home"
    bag = make_bag(cwd / "run", {"/t": [1.0, 2.0]})
    (bag / "metadata.yaml").unlink()  # live: storage present, no metadata
    _fake_proc(tmp_path, monkeypatch, 300,
               RECORD_ARGV + ["-o", "run", "/t"], cwd=cwd)
    found = recorder_scan.scan()
    assert [r["output_dir"] for r in found] == [bag.resolve()]


def test_scan_warns_once_when_output_unresolvable(tmp_path, monkeypatch,
                                                  caplog):
    cwd = tmp_path / "home"
    cwd.mkdir()  # nothing being written anywhere findable
    _fake_proc(tmp_path, monkeypatch, 400, RECORD_ARGV + ["/t"], cwd=cwd)
    with caplog.at_level("WARNING", logger="fair_ros.watchdog.recorder_scan"):
        assert recorder_scan.scan() == []
        assert recorder_scan.scan() == []  # second poll: no repeat
    warnings = [r for r in caplog.records
                if "could not be resolved" in r.message]
    assert len(warnings) == 1
    assert "400" in warnings[0].getMessage()


def test_record_pids_ignores_non_recorders(tmp_path, monkeypatch):
    _fake_proc(tmp_path, monkeypatch, 500,
               ["python3", "ros2", "bag", "play", "x"])
    assert recorder_scan.record_pids() == []


# -- watchdog foreign detection ------------------------------------------------

def _foreign_dog(found):
    ino, clock = FakeINotify(), FakeClock()
    dog = Watchdog(inotify=ino, clock=clock, pipeline=good_pipeline,
                   harvest_in_thread=False, scan_recorders=lambda: found)
    dog.start()
    return ino, clock, dog


def test_foreign_recording_detected_and_finalised(fair_dirs, tmp_path):
    foreign = make_bag(tmp_path / "ext_run", {"/fix": _steady(T0, T0 + 60, 10)})
    found = [{"pid": os.getpid(), "output_dir": foreign, "discovery": {}}]
    ino, clock, dog = _foreign_dog(found)

    clock.now += SCAN_S  # let the poller fire
    dog.step(0)
    assert dog.state == RECORDING
    assert dog.active_bag_dir == foreign
    assert foreign in dog._foreign

    ino.emit(foreign, flags.CLOSE_WRITE, "metadata.yaml")
    dog.step(0)
    assert dog.state == IDLE
    harvest, _ = builder.load_spool()
    assert [b["source"] for b in harvest["bags"]] == ["detected"]
    assert harvest["bags"][0]["path"] == str(foreign)
    assert foreign.is_dir()  # referenced in place, not moved


def test_foreign_recorder_exit_finalises(fair_dirs, tmp_path):
    foreign = make_bag(tmp_path / "ext2", {"/fix": _steady(T0, T0 + 60, 10)})
    dead_pid = 0x7FFFFFFF  # no such process
    found = [{"pid": dead_pid, "output_dir": foreign, "discovery": {}}]
    _, clock, dog = _foreign_dog(found)

    clock.now += SCAN_S
    dog.step(0)  # poller adopts, then the recorder-exit hint finalises it
    assert dog.state == IDLE
    harvest, _ = builder.load_spool()
    assert harvest["bags"][0]["source"] == "detected"


def test_foreign_harvest_adopts_recorder_environ(fair_dirs, tmp_path):
    foreign = make_bag(tmp_path / "ext3", {"/fix": _steady(T0, T0 + 5, 10)})
    found = [{"pid": os.getpid(), "output_dir": foreign,
              "discovery": {"ROS_DOMAIN_ID": "42"}}]
    ino, clock, dog = _foreign_dog(found)
    with mock.patch.dict(wd_mod.os.environ, {"ROS_DOMAIN_ID": "0"}, clear=False):
        clock.now += SCAN_S
        dog.step(0)
        assert dog.state == RECORDING
        # the harvest ran on the recorder's partition, not the watchdog default
        assert wd_mod.os.environ["ROS_DOMAIN_ID"] == "42"


def test_foreign_recording_queued_while_busy(fair_dirs, tmp_path):
    foreign = make_bag(tmp_path / "ext4", {"/fix": _steady(T0, T0 + 60, 10)})
    found = [{"pid": os.getpid(), "output_dir": foreign, "discovery": {}}]
    ino, clock, dog = _foreign_dog(found)

    # A spool recording is already in progress.
    bag_a = make_bag(paths.bags_dir() / "rosbag2_a",
                     {"/fix": _steady(T0, T0 + 60, 10)})
    ino.emit_dir_created(paths.bags_dir(), "rosbag2_a")
    dog.step(0)
    ino.emit_file(bag_a, "rosbag2_a_0.db3")
    dog.step(0)
    assert dog.active_bag_dir == bag_a

    clock.now += SCAN_S
    dog.step(0)
    assert dog.active_bag_dir == bag_a       # not pre-empted
    assert foreign in dog.queued_bags        # one bag, one mission


def test_poller_ignores_spool_bags(fair_dirs):
    dog = Watchdog(inotify=FakeINotify(), pipeline=good_pipeline,
                   harvest_in_thread=False, scan_recorders=lambda: [])
    spool_bag = (paths.bags_dir() / "rosbag2_x").resolve()
    assert dog._is_tracked(spool_bag)               # inotify already covers it
    assert not dog._is_tracked((paths.archive_dir() / "elsewhere").resolve())


# -- ros2 fair adopt -----------------------------------------------------------

def _seed_harvest():
    fsio.atomic_write_json(paths.harvest_json_path(), good_pipeline())


def _args(bagdir, json=False):
    return SimpleNamespace(bagdir=str(bagdir), json=json, debug=False)


def test_adopt_appends_bag(fair_dirs, tmp_path):
    _seed_harvest()
    bag = make_bag(tmp_path / "adopt_me", {"/fix": _steady(T0, T0 + 30, 10)})
    assert adopt.run(_args(bag)) == 0
    harvest, _ = builder.load_spool()
    assert harvest["bags"][-1]["source"] == "adopted"
    assert harvest["bags"][-1]["path"] == str(bag.resolve())


def test_adopt_rejects_non_bag(fair_dirs, tmp_path):
    assert adopt.run(_args(tmp_path / "nope")) == 1
    empty = tmp_path / "empty"
    empty.mkdir()
    assert adopt.run(_args(empty)) == 1  # a dir, but not a recording


def test_adopt_refuses_while_recording(fair_dirs, tmp_path):
    fsio.atomic_write_json(paths.watchdog_state_path(),
                           {"version": 1, "state": "RECORDING"})
    bag = make_bag(tmp_path / "busy", {"/fix": [T0, T0 + 1]})
    assert adopt.run(_args(bag)) == 1


def test_adopt_is_idempotent(fair_dirs, tmp_path):
    _seed_harvest()
    bag = make_bag(tmp_path / "twice", {"/fix": [T0, T0 + 1]})
    assert adopt.run(_args(bag)) == 0
    assert adopt.run(_args(bag)) == 0
    harvest, _ = builder.load_spool()
    assert sum(1 for b in harvest["bags"]
               if b["path"] == str(bag.resolve())) == 1


def test_adopt_harvests_when_no_context(fair_dirs, tmp_path, monkeypatch):
    monkeypatch.setattr(adopt.watchdog, "run_pipeline", good_pipeline)
    bag = make_bag(tmp_path / "cold", {"/fix": [T0, T0 + 1]})
    assert adopt.run(_args(bag)) == 0
    harvest, _ = builder.load_spool()
    assert harvest is not None
    assert harvest["bags"][-1]["source"] == "adopted"


# -- assembler: copy foreign, drop vanished ------------------------------------

def _bag_entry(bag_path, source):
    return {
        "path": str(bag_path), "source": source, "storage_format": "sqlite3",
        "size_bytes": fsio.dir_size_bytes(bag_path), "start_time": None,
        "end_time": None, "duration_s": None, "message_count": 0,
        "topics": [], "health_warnings": [],
    }


def _record_with_bags(harvest, *bags):
    harvest["bags"] = list(bags)
    context = builder.new_mission_context("Op", "Goal", "Loc")
    return builder.build(harvest, context), harvest


def _record_with_bag(bag_path, source):
    return _record_with_bags(good_pipeline(),
                             _bag_entry(bag_path, source))


def test_foreign_bag_copied_not_moved(fair_dirs, tmp_path):
    bag = make_bag(tmp_path / "ext", {"/fix": [T0, T0 + 1]})
    record, harvest = _record_with_bag(bag, "detected")
    crate = assembler.assemble(record, harvest)
    assert (crate / "bags" / "ext" / "metadata.yaml").is_file()
    assert bag.is_dir()  # original left in place


def test_vanished_foreign_bag_skipped(fair_dirs, tmp_path):
    bag = make_bag(tmp_path / "gone", {"/fix": [T0, T0 + 1]})
    record, harvest = _record_with_bag(bag, "detected")
    shutil.rmtree(bag)  # operator moved/deleted it before saving
    crate = assembler.assemble(record, harvest)
    assert not (crate / "bags" / "gone").exists()
    assert record.bags == []


def test_spool_bag_still_moved(fair_dirs):
    bag = make_bag(paths.bags_dir() / "rosbag2_m", {"/fix": [T0, T0 + 1]})
    record, harvest = _record_with_bag(bag, "mission_record")
    crate = assembler.assemble(record, harvest)
    assert (crate / "bags" / "rosbag2_m").is_dir()
    assert not bag.exists()  # moved out of the spool


def test_foreign_bag_vanishing_mid_assembly_is_not_fatal(
        fair_dirs, tmp_path, monkeypatch):
    """A foreign bag deleted *during* assembly is dropped, not fatal (#35)."""
    foreign = make_bag(tmp_path / "ext", {"/fix": [T0, T0 + 1]})
    spool = make_bag(paths.bags_dir() / "rosbag2_keep", {"/fix": [T0, T0 + 1]})
    record, harvest = _record_with_bags(
        good_pipeline(),
        _bag_entry(foreign, "detected"),
        _bag_entry(spool, "mission_record"))

    def vanishing_copytree(src, dst, *a, **k):
        shutil.rmtree(src)  # operator deletes it just as the copy begins
        raise FileNotFoundError(src)
    monkeypatch.setattr(assembler.shutil, "copytree", vanishing_copytree)

    crate = assembler.assemble(record, harvest)
    # the save completed; the spool bag survived and the foreign one was dropped
    assert (crate / "bags" / "rosbag2_keep").is_dir()
    assert not (crate / "bags" / "ext").exists()
    assert [b.source for b in record.bags] == ["mission_record"]
    # and the crate is internally consistent (manifest matches what's on disk)
    assert all((crate / b.path).is_dir() for b in record.bags)


# -- mission_status: live-recorder surfacing ------------------------------------

def _outside(state, recs, pids):
    from fair_ros.ui import status as status_ui
    return status_ui.outside_recordings(state, recorders=recs,
                                        matched_pids=pids)


def test_outside_recordings_statuses(fair_dirs, tmp_path):
    a, b, c, d = (tmp_path / n for n in "abcd")
    harvest = good_pipeline()
    harvest["bags"] = [_bag_entry(c, "detected")]
    fsio.atomic_write_json(paths.harvest_json_path(), harvest)
    state = {"active_bag_dir": str(a), "queued_bags": [str(b)]}
    recs = [{"pid": 1, "output_dir": a}, {"pid": 2, "output_dir": b},
            {"pid": 3, "output_dir": c}, {"pid": 4, "output_dir": d},
            {"pid": 5, "output_dir": paths.bags_dir() / "spool_bag"}]
    out = _outside(state, recs, [1, 2, 3, 4, 5, 9])
    assert {r["path"]: r["status"] for r in out["recordings"]} == {
        str(a): "capturing", str(b): "queued",
        str(c): "captured", str(d): "missed"}  # spool bag excluded
    assert out["unresolved_pids"] == [9]


def test_outside_lines_plain_language(fair_dirs, tmp_path):
    from fair_ros.ui import status as status_ui
    out = _outside({"active_bag_dir": None, "queued_bags": []},
                   [{"pid": 1, "output_dir": tmp_path / "x"}], [1, 2])
    lines = status_ui.outside_lines(out)
    assert any("NOT captured" in line for line in lines)
    assert any("can't see where it saves" in line for line in lines)


def test_status_as_dict_has_live_recorders(fair_dirs):
    from fair_ros.ui import status as status_ui
    doc = status_ui.status_as_dict(None, None)
    assert doc["live_recorders"] == {"recordings": [], "unresolved_pids": []}


def test_show_status_renders_outside_recording(fair_dirs, tmp_path):
    import io

    from rich.console import Console

    from fair_ros.ui import status as status_ui
    out = _outside({}, [{"pid": 1, "output_dir": tmp_path / "run"}], [1])
    console = Console(file=io.StringIO(), width=120)
    status_ui.show_status(None, None, console=console, outside=out)
    assert "Outside recording" in console.file.getvalue()


# -- builder: vanished-foreign warning -----------------------------------------

def _harvest_with_foreign(path):
    harvest = good_pipeline()
    harvest["bags"] = [{"path": str(path), "source": "detected"}]
    return harvest


def test_warns_on_vanished_foreign(fair_dirs):
    warns = builder.harvest_level_warnings(
        _harvest_with_foreign("/no/such/run"))
    assert any("can no longer be found" in w for w in warns)


def test_no_warning_for_present_foreign(fair_dirs, tmp_path):
    bag = make_bag(tmp_path / "here", {"/fix": [T0]})
    warns = builder.harvest_level_warnings(_harvest_with_foreign(bag))
    assert not any("can no longer be found" in w for w in warns)
