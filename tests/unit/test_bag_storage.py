import importlib.util

import pytest

from fair_ros.utils import bag_storage
from tests.conftest import make_bag, make_mcap_bag

T0 = 1_750_000_000.0

_MCAP_PRESENT = importlib.util.find_spec("mcap") is not None


def test_get_reader_dispatch():
    assert isinstance(bag_storage.get_reader("sqlite3"),
                      bag_storage.SqliteReader)
    assert isinstance(bag_storage.get_reader("mcap"), bag_storage.McapReader)
    assert bag_storage.get_reader("rosbag_v9000") is None


def test_supports_timestamps():
    assert bag_storage.supports_timestamps("sqlite3") is True
    # MCAP support tracks whether the optional mcap package is installed.
    assert bag_storage.supports_timestamps("mcap") is _MCAP_PRESENT
    assert bag_storage.supports_timestamps("unknown") is False


def test_sqlite_reader_returns_sorted_series(tmp_path):
    bag = make_bag(tmp_path / "bag", {
        "/fix": [T0 + 2, T0, T0 + 1],   # deliberately out of order
        "/depth": [T0, T0 + 0.5],
    })
    series = bag_storage.SqliteReader().topic_timestamps(
        bag, [f"{bag.name}_0.db3"])
    assert series["/fix"] == [T0, T0 + 1, T0 + 2]
    assert series["/depth"] == [T0, T0 + 0.5]


def test_sqlite_reader_falls_back_to_glob(tmp_path):
    """A bag whose metadata omits relative_file_paths still resolves .db3."""
    bag = make_bag(tmp_path / "bag", {"/fix": [T0, T0 + 1]})
    series = bag_storage.SqliteReader().topic_timestamps(bag, [])
    assert series["/fix"] == [T0, T0 + 1]


@pytest.mark.skipif(not _MCAP_PRESENT, reason="mcap package not installed")
def test_mcap_reader_returns_sorted_series(tmp_path):
    bag = make_mcap_bag(tmp_path / "bag", {
        "/fix": [T0 + 2, T0, T0 + 1],   # writer preserves insertion order
        "/depth": [T0, T0 + 0.5],
    })
    series = bag_storage.McapReader().topic_timestamps(
        bag, [f"{bag.name}_0.mcap"])
    assert series["/fix"] == [T0, T0 + 1, T0 + 2]
    assert series["/depth"] == [T0, T0 + 0.5]


@pytest.mark.skipif(not _MCAP_PRESENT, reason="mcap package not installed")
def test_mcap_reader_falls_back_to_glob(tmp_path):
    bag = make_mcap_bag(tmp_path / "bag", {"/fix": [T0, T0 + 1]})
    series = bag_storage.McapReader().topic_timestamps(bag, [])
    assert series["/fix"] == [T0, T0 + 1]


@pytest.mark.skipif(not _MCAP_PRESENT, reason="mcap package not installed")
def test_mcap_reader_skips_corrupt_file(tmp_path):
    bag = tmp_path / "bag"
    bag.mkdir()
    (bag / "bag_0.mcap").write_bytes(b"not a real mcap file")
    assert bag_storage.McapReader().topic_timestamps(bag, []) == {}
