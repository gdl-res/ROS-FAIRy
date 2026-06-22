import io
import json
from types import SimpleNamespace

from rich.console import Console

from fair_ros.archive import assembler, index
from fair_ros.manifest import builder
from fair_ros.subcommands import verify
from fair_ros.utils import paths
from tests.unit.test_archive import _spool

FAIL, WARN, OK = verify.FAIL, verify.WARN, verify.OK


def _make_crate(fair_dirs):
    harvest, context = _spool(fair_dirs)
    record = builder.build(harvest, context)
    return assembler.assemble(record, harvest)


def _statuses(checks):
    return [c["status"] for c in checks]


def _console():
    return Console(file=io.StringIO(), width=100, force_terminal=False)


def test_clean_archive_has_no_failures(fair_dirs):
    crate = _make_crate(fair_dirs)
    checks = verify.verify_archive(crate)
    assert FAIL not in _statuses(checks)
    assert verify._overall(checks) != FAIL
    # the substantive checks are present
    titles = " ".join(c["title"] for c in checks)
    assert "Mission record is valid" in titles
    assert "matches its checksum" in titles
    assert "is complete" in titles
    assert "registered in the index" in titles


def test_run_returns_zero_for_clean_archive(fair_dirs):
    _make_crate(fair_dirs)
    console = _console()
    args = SimpleNamespace(mission="1", json=False, debug=False)
    assert verify.run(args, console=console) == 0
    assert "PASS" in console.file.getvalue()


def test_detects_modified_calibration(fair_dirs):
    crate = _make_crate(fair_dirs)
    cal = next(crate.glob("calibrations/*"))
    cal.write_text("tampered: true\n")
    checks = verify.verify_archive(crate)
    bad = [c for c in checks if c["status"] == FAIL]
    assert any("Calibration" in c["title"] and "modified" in c["title"]
               for c in bad)
    assert verify._overall(checks) == FAIL


def test_detects_missing_bag_data_file(fair_dirs):
    crate = _make_crate(fair_dirs)
    db = next(crate.glob("bags/*/*.db3"))
    db.unlink()
    checks = verify.verify_archive(crate)
    assert any(c["status"] == FAIL and "missing data files" in c["title"]
               for c in checks)


def test_detects_missing_referenced_file(fair_dirs):
    crate = _make_crate(fair_dirs)
    (crate / "harvest" / "pip_freeze.txt").unlink()
    checks = verify.verify_archive(crate)
    assert any(c["status"] == FAIL
               and "referenced by the crate are missing" in c["title"]
               for c in checks)


def test_not_in_index_warns_but_passes(fair_dirs):
    crate = _make_crate(fair_dirs)
    paths.index_db_path().unlink()  # simulate a lost/rebuilt index
    checks = verify.verify_archive(crate)
    assert FAIL not in _statuses(checks)
    assert any(c["status"] == WARN and "not in the local index" in c["title"]
               for c in checks)
    assert verify._overall(checks) == WARN


def test_corrupt_mission_record_fails_fast(fair_dirs):
    crate = _make_crate(fair_dirs)
    (crate / "mission_record.json").write_text("{ not valid json")
    checks = verify.verify_archive(crate)
    assert len(checks) == 1
    assert checks[0]["status"] == FAIL


def test_json_output(fair_dirs, capsys):
    _make_crate(fair_dirs)
    args = SimpleNamespace(mission="1", json=True, debug=False)
    assert verify.run(args, console=_console()) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"] in (OK, WARN)
    assert payload["checks"] and "archive" in payload


def test_unknown_mission_errors(fair_dirs):
    index._connect().close()  # ensure an (empty) index exists
    console = _console()
    args = SimpleNamespace(mission="does-not-exist", json=False, debug=False)
    assert verify.run(args, console=console) == 1
    assert "Can't find a mission" in console.file.getvalue()
