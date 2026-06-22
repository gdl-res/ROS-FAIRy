#!/usr/bin/env python3
"""Re-stamp an MCAP rosbag2 bag whose record clock was broken.

ros2 bag play honours each message's log_time. When the recording clock was
unsynced, most messages are stamped near the epoch (1970) and a few at the real
time, so the player tries to replay a ~56-year timeline and appears to freeze.

This rewrites a NEW bag (originals untouched) assigning a dense, monotonic
log_time/publish_time in the messages' original write order, spread over
--duration seconds. Message bytes, topics, types and order are preserved; only
timing is synthetic (and therefore approximate).

Usage: restamp_bag.py <in_bag_dir> <out_bag_dir> [--duration SECONDS]
"""
import argparse
import sys
import time
from pathlib import Path

from mcap.reader import make_reader
from mcap.writer import Writer

GOOD_FLOOR_NS = 946684800 * 10**9  # 2000-01-01


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("in_bag", type=Path)
    ap.add_argument("out_bag", type=Path)
    ap.add_argument("--duration", type=float, default=None,
                    help="target playback length (s); default = span of the "
                         "few good stamps, else 60s")
    args = ap.parse_args()

    src = next(args.in_bag.glob("*.mcap"), None)
    if src is None:
        print(f"no .mcap in {args.in_bag}", file=sys.stderr)
        return 1

    # Pass 1: count messages and find any real-stamp span to size duration.
    n = 0
    good = []
    with open(src, "rb") as f:
        for _s, _c, m in make_reader(f).iter_messages(log_time_order=False):
            n += 1
            if m.log_time >= GOOD_FLOOR_NS:
                good.append(m.log_time)
    if not n:
        print("empty bag", file=sys.stderr)
        return 1
    if args.duration is not None:
        dur_ns = int(args.duration * 1e9)
    elif len(good) >= 2:
        dur_ns = max(good) - min(good)
    else:
        dur_ns = 60 * 10**9
    step = max(1, dur_ns // n)
    base = time.time_ns()  # a plausible recent epoch so the bag also looks sane

    args.out_bag.mkdir(parents=True, exist_ok=True)
    out = args.out_bag / f"{args.out_bag.name}_0.mcap"

    schema_map: dict[int, int] = {}
    channel_map: dict[int, int] = {}
    with open(src, "rb") as f, open(out, "wb") as g:
        reader = make_reader(f)
        w = Writer(g)
        w.start()
        i = 0
        for schema, channel, message in reader.iter_messages(log_time_order=False):
            if schema and schema.id not in schema_map:
                schema_map[schema.id] = w.register_schema(
                    name=schema.name, encoding=schema.encoding, data=schema.data)
            if channel.id not in channel_map:
                channel_map[channel.id] = w.register_channel(
                    topic=channel.topic,
                    message_encoding=channel.message_encoding,
                    schema_id=schema_map.get(channel.schema_id, 0),
                    metadata=dict(channel.metadata))
            ts = base + i * step
            w.add_message(channel_id=channel_map[channel.id],
                          log_time=ts, publish_time=ts,
                          sequence=message.sequence, data=message.data)
            i += 1
        w.finish()
    print(f"re-stamped {n} messages over {dur_ns/1e9:.1f}s -> {out}")
    print("NOTE: write a metadata.yaml with `ros2 bag reindex` before playing:")
    print(f"  ros2 bag reindex {args.out_bag} mcap")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
