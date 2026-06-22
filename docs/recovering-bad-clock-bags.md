# Recovering bags recorded with an unsynchronised clock

## Symptom

`ros2 bag play <bag>` freezes the console and `ros2 topic echo` shows nothing.
`ros2 fair verify`/`mission_close` report the recording's duration as unknown
with an "unreliable clock" warning, or the duration looks like ~56 years.

## Cause

If `ros2 bag record` (or the ROS nodes) start before NTP/chrony has stepped the
system clock, most messages are stamped near the epoch (1970) while a few carry
the real time once the clock syncs. `ros2 bag play` honours those timestamps, so
it tries to replay a ~56-year timeline — it bursts the 1970 messages, then
sleeps "until now", which looks like a frozen console. Both `log_time` and
`publish_time` (and usually the message header stamps) are affected, so the real
timing is **not recoverable** from the bag.

## Prevention (do this)

Make sure the clock is synchronised **before** recording. `ros2 fair
mission_record` now refuses to record on an unsynced clock unless you confirm
(`utils/clock.py`); to gate your own recording scripts:

```bash
timedatectl show -p NTPSynchronized --value   # -> yes / no
# or, to block until synced:
chronyc waitsync 60 0.01
```

Order the recording to start after time-sync (e.g. `After=time-sync.target` /
`chrony.service` in a systemd unit, or a `chronyc waitsync` guard in a launch
wrapper).

## Recovering an existing bad bag

Use `ros2 fair repair` — it writes **new**, immediately-playable copies of the
affected recordings (originals untouched, so checksums and `verify` still hold)
and regenerates each `metadata.yaml`, so no `ros2 bag reindex` step is needed.

```bash
ros2 fair repair 1 -o ~/repaired     # mission 1 (newest); --all to force every bag
ros2 bag play ~/repaired/<bag-name>  # now plays; echo shows data
```

It also accepts a path to a single bag directory. For a bare machine without a
sourced ROS/fair-ros environment, `tools/restamp_bag.py` is a thin wrapper over
the same code:

```bash
python3 tools/restamp_bag.py <bad_bag_dir> <new_bag_dir> --duration 120
ros2 bag play <new_bag_dir>
```

**The new timing is synthetic.** Messages keep their original order, types and
bytes, but inter-message timing is fabricated (spread evenly over `--duration`).
This is fine for eyeballing topics, checking message contents, and confirming a
sensor recorded data — but it is **not** safe for anything that trusts
timestamps (SLAM, sensor fusion, latency analysis). A broken-clock recording is
fundamentally compromised for time-critical use; the only real fix is to sync
the clock before recording.
