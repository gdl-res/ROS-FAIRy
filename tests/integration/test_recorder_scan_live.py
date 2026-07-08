"""End-to-end check of the foreign-recorder scan against the real /proc.

Spawns a fake recorder process whose cmdline reads ``... bag record ...`` and
which holds a storage file open, exactly like rosbag2 — no ROS, no root, no
mocks. This is the layer the fake-inotify unit tests cannot cover: that
``recorder_scan.scan()`` really resolves a live process from /proc.
"""

import subprocess
import sys
import textwrap

from fair_ros.watchdog import recorder_scan

FAKE_RECORDER = textwrap.dedent("""\
    import sys, time
    from pathlib import Path
    out = Path(sys.argv[sys.argv.index("-o") + 1])
    out.mkdir(parents=True, exist_ok=True)
    f = open(out / "fake_0.mcap", "wb")
    print("ready", flush=True)
    time.sleep(120)
""")


def test_scan_finds_live_recorder_process(tmp_path):
    script = tmp_path / "fake_recorder.py"
    script.write_text(FAKE_RECORDER)
    out_dir = tmp_path / "run42"
    proc = subprocess.Popen(
        [sys.executable, str(script), "bag", "record", "-o", str(out_dir)],
        stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        assert proc.pid in recorder_scan.record_pids()
        found = {r["pid"]: r for r in recorder_scan.scan()}
        assert proc.pid in found
        assert found[proc.pid]["output_dir"] == out_dir.resolve()
        assert recorder_scan.pid_alive(proc.pid)
    finally:
        proc.kill()
        proc.wait()
    assert not recorder_scan.pid_alive(proc.pid)
